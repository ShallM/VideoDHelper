import concurrent.futures
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import uuid

from common import UserError, read_json, write_json
from summary import ChatClient, OutputLimit
from usage import UsageLedger, normalize_usage
from worker import load_config, run


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, _):
        return json.dumps(self.value).encode()


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = {"baseURL": "https://example.com/v1", "model": "demo", "apiKey": "test-only-secret",
                       "providerID": "provider", "providerName": "Demo", "modelConfigID": "model", "modelName": "Demo model",
                       "usageRoot": str(self.root), "pricing": {"currency": "CNY", "inputPerMillion": 2, "outputPerMillion": 8}}
        self.response = {"usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                                   "prompt_tokens_details": {"cached_tokens": 30}, "completion_tokens_details": {"reasoning_tokens": 25}},
                         "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}
    def tearDown(self):
        self.temporary.cleanup()
    def events(self):
        return [read_json(p) for p in (self.root / "usage/events").glob('*.json')]
    def client(self):
        return ChatClient(self.config, self.root / "cache", lambda *args: None)
    def test_normalization_shared_with_native_app(self):
        fixtures = Path(__file__).resolve().parents[2] / "Tests/Fixtures/token-usage.json"
        for case in read_json(fixtures):
            with self.subTest(case=case['name']):
                self.assertEqual(normalize_usage(case['raw']), case['expected'])

    def test_completed_cache_hit_has_no_new_request_or_bill(self):
        client = self.client()
        opener = Mock(); opener.open.return_value = Response(self.response)
        with patch('summary.urllib.request.build_opener', return_value=opener):
            for _ in range(2):
                self.assertEqual(client.complete('prompt', {}, lambda x: self.assertTrue(x['ok'])), {'ok': True})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['usage']['reportedTotalTokens'], 140)
        log = read_json_lines(self.root / 'cache/usage.jsonl')
        self.assertEqual(log[0]['eventID'], self.events()[0]['id'])
        self.assertEqual(self.events()[0]['pricing']['inputPerMillion'], 2)
        self.assertNotIn('test-only-secret', ''.join(p.read_text() for p in self.root.rglob('*.json*')))

    def test_each_network_retry_is_recorded_even_without_token_usage(self):
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('https://example.com/v1', 429, 'limited', {}, io.BytesIO(b'{}')), Response(self.response)]
        with patch('summary.urllib.request.build_opener', return_value=opener), patch('summary.time.sleep'):
            self.client().complete('prompt', {}, lambda x: None)
        events = self.events()
        self.assertEqual(len(events), 2)
        failed = next(e for e in events if e['status'] == 'http_error')
        self.assertEqual(failed['usage'], {'invalid': False})
        self.assertEqual(failed['httpStatus'], 429)
        self.assertEqual(sum(e['usage'].get('inputTokens', 0) for e in events), 100)

    def test_output_limit_and_empty_choices_still_record_metering(self):
        for case, value in enumerate([{**self.response, 'choices': [{'finish_reason': 'length', 'message': {'content': ''}}]}, {**self.response, 'choices': []}]):
            opener = Mock(); opener.open.return_value = Response(value)
            with patch('summary.urllib.request.build_opener', return_value=opener), self.assertRaises((OutputLimit, UserError)):
                self.client().complete('prompt', {'case': case}, lambda x: None)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(sum(e['usage']['reportedTotalTokens'] for e in self.events()), 280)

    def test_in_flight_attempt_survives_interruption_as_unknown(self):
        ledger = UsageLedger(self.config); event = ledger.begin('chunk')
        self.config['pricing']['inputPerMillion'] = 999
        saved = self.events()[0]
        self.assertEqual(saved['id'], event['id'])
        self.assertEqual(saved['status'], 'in_flight')
        self.assertNotIn('inputTokens', saved['usage'])
        self.assertEqual(saved['pricing']['inputPerMillion'], 2)

    def test_concurrent_writers_are_atomic_and_private(self):
        ledger = UsageLedger(self.config)
        def record(i):
            event = ledger.begin('chunk'); ledger.finish(event, 'response', {'prompt_tokens': i, 'completion_tokens': 2}, 200, 'stop')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(24)))
        self.assertEqual(len(self.events()), 24)
        self.assertEqual(sum(e['usage']['inputTokens'] for e in self.events()), sum(range(24)))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in (self.root / 'usage/events').glob('*.json')))

    def test_usage_logs_never_copy_extra_response_fields(self):
        self.response['usage']['unknown_sensitive_field'] = 'never-copy-this'
        self.response['choices'][0]['message']['reasoning_content'] = 'private-reasoning'
        opener = Mock(); opener.open.return_value = Response(self.response)
        with patch('summary.urllib.request.build_opener', return_value=opener):
            self.client().complete('prompt', {}, lambda x: None)
        contents = ''.join(p.read_text() for p in self.root.rglob('*.json*'))
        self.assertNotIn('never-copy-this', contents); self.assertNotIn('private-reasoning', contents)

    def preferences(self, model='first'):
        return {'schemaVersion': 1, 'defaultProviderID': 'p', 'defaultModelID': 'm', 'general': {'summaryConcurrency': 1},
                'providers': [{'id': 'p', 'name': 'Demo', 'baseURL': 'https://example.com/v1', 'apiKey': 'test-only', 'enabled': True,
                               'models': [{'id': 'm', 'modelID': model, 'name': model, 'reasoningEffort': 'default', 'pricing': {'currency': 'CNY'}}]}]}

    def test_new_settings_override_legacy_and_invalid_defaults_do_not_fall_back(self):
        write_json(self.root / 'settings.json', {'model': 'legacy', 'apiKey': 'legacy-key'})
        write_json(self.root / 'preferences.json', self.preferences())
        config = load_config(self.root)
        self.assertEqual(config['model'], 'first'); self.assertEqual(config['concurrency'], 1)
        invalid = self.preferences(); invalid['providers'][0]['enabled'] = False
        write_json(self.root / 'preferences.json', invalid)
        with self.assertRaises(UserError):
            load_config(self.root)

    def test_worker_snapshots_configuration_before_media_processing(self):
        folder = self.root / str(uuid.uuid4()); folder.mkdir()
        write_json(self.root / 'preferences.json', self.preferences())
        request = {'title': 'fixture', 'url': 'https://www.bilibili.com/video/BV1xx411c7mD/', 'source': 'test',
                   'segments': [{'id': 0, 'start': 0, 'end': 1, 'text': '测试'}], 'mediaMode': 'audio', 'keepMedia': True, 'useWhisper': False}
        write_json(folder / 'request.json', request); write_json(folder / 'job.json', {'id': folder.name, 'state': 'pending'})
        def download(*args):
            write_json(self.root / 'preferences.json', self.preferences('changed'))
        seen = []
        def summarize(segments, title, config, folder, emit):
            seen.append(config['model']); return {}, []
        with patch('worker.download', side_effect=download), patch('worker.summarize', side_effect=summarize), patch('worker.export_notes'):
            self.assertEqual(run(folder, self.root), 0)
        self.assertEqual(seen, ['first'])
        self.assertEqual(load_config(self.root)['model'], 'changed')


def read_json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]
