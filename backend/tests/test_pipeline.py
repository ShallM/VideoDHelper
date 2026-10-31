import copy
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

from common import UserError, clean_media, read_json, save_transcript, validate_segments, write_json, write_text
from media import owned_words, pause_boundaries
from summary import (ChatClient, CHUNK_PROMPT, OVERVIEW_PROMPT, REDUCE_PROMPT, MERGE_PROMPT, OutputLimit,
                     batches, encoded, summarize, validate_chunk, validate_overview, ordered_parallel, stitch_notes,
                     outline_items, expand_note)
from worker import run


def segments(count=12):
    return [{"id": i, "start": i * 30., "end": i * 30. + 20, "text": f"技术片段 {i}，学习率 3e-4，只在该条件下有效。"} for i in range(count)]


def chunk_value(values):
    return {"condensed": [{"source_ids": [s["id"]], "text": s["text"]} for s in values],
            "notes": [{"source_ids": [s["id"] for s in values], "title": "学习率", "detail": "学习率 3e-4，仅限给定条件。"}]}


class FakeClient:
    seen = []
    split_limit = 100000
    long_notes = False
    def __init__(self, config, directory, emit):
        self.total_tokens = 99
    def complete(self, prompt, payload, validator, output_tokens=8192):
        self.seen.append((prompt, copy.deepcopy(payload)))
        if prompt == CHUNK_PROMPT:
            if len(payload['segments']) > self.split_limit:
                raise OutputLimit()
            value = chunk_value(payload['segments'])
            if self.long_notes:
                value['notes'][0]['detail'] = '技术细节必须保留。' * 180
        elif prompt == MERGE_PROMPT:
            value = {'merge': False}
        elif prompt == REDUCE_PROMPT:
            allowed = sorted(n['note_id'] for n in payload)
            value = {'notes': [{'note_ids': allowed, 'title': '汇总', 'detail': '条件与限制。'}]}
        else:
            allowed = sorted(n['note_id'] for n in payload['notes'])
            value = {'overview': '来源概览。', 'key_points': [{'note_ids': allowed, 'text': '有条件的结论。'}], 'uncertainties': []}
        validator(value)
        return value


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        FakeClient.seen, FakeClient.split_limit, FakeClient.long_notes = [], 100000, False
    def tearDown(self):
        self.tmp.cleanup()

    def test_coverage_rejects_missing_duplicate_reordered_and_context(self):
        valid = chunk_value(segments(4))
        validate_chunk(valid, list(range(4)), {4, 5})
        for ids in ([0, 1, 2], [0, 1, 2, 3, 3], [1, 0, 2, 3], [0, 1, 2, 3, 4], [True, 1, 2, 3]):
            invalid = copy.deepcopy(valid)
            invalid['condensed'] = [{'source_ids': ids, 'text': 'x'}]
            with self.assertRaises(ValueError):
                validate_chunk(invalid, list(range(4)), {4, 5})

    def test_context_can_be_cited_in_notes_but_not_condensed(self):
        valid = chunk_value(segments(4))
        valid['notes'][0]['source_ids'] += [4]
        validate_chunk(valid, list(range(4)), {4})
        with self.assertRaises(ValueError):
            validate_chunk(valid, list(range(4)), set())

    def test_pause_cut_avoids_losing_a_word_to_cross_window_timing_drift(self):
        left = {'segments': [{'words': [{'word': '脸', 'start': 239.95, 'end': 240.15}]}]}
        right = {'segments': [{'words': [{'word': '脸', 'start': 7.75, 'end': 8.05}]}]}
        # The old cut loses the word in both windows when their midpoint estimates differ.
        self.assertEqual(owned_words(left, 0, 0, 240) + owned_words(right, 232, 240, 480), [])
        cuts, snapped = pause_boundaries(480, [(240.4, 240.82)])
        fixed = owned_words(left, 0, 0, cuts[1]) + owned_words(right, 232, cuts[1], 480)
        self.assertEqual(''.join(s['text'] for s in fixed), '脸')
        self.assertEqual(snapped, 1)

    def test_pause_plan_partitions_full_audio_and_records_fallbacks(self):
        duration = 5224.959
        cuts, snapped = pause_boundaries(duration, [(238, 238.5), (480.1, 480.5), (719, 719.1), (550, 551)])
        self.assertEqual(cuts[:4], [0, 238.25, 480.3, 720])
        self.assertEqual(snapped, 2)
        self.assertEqual(cuts[-1], duration)
        self.assertTrue(all(a < b for a, b in zip(cuts, cuts[1:])))
        self.assertAlmostEqual(sum(b-a for a,b in zip(cuts,cuts[1:])), duration)
        self.assertEqual(pause_boundaries(240.2, [])[0], [0, 240.2])

    def test_parallel_summaries_are_bounded_and_return_source_order(self):
        active = peak = 0
        lock = threading.Lock()
        counts = []
        def operation(index):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01 * (3 - index % 3))
            with lock:
                active -= 1
            return index
        result = ordered_parallel(list(range(7)), operation, lambda a,b: counts.append((a,b)), threading.Event())
        self.assertEqual(result, list(range(7)))
        self.assertEqual(peak, 2)
        self.assertEqual(counts, [(i,7) for i in range(1,8)])

    def test_failed_parallel_chunk_stops_dispatch_and_preserves_successful_cache(self):
        cancelled, release = threading.Event(), threading.Event()
        seen = []
        def operation(index):
            seen.append(index)
            if index == 0:
                write_json(self.root / 'completed.json', {'complete': True})
                release.set()
                raise UserError('API unavailable')
            release.wait(1)
            return index
        with self.assertRaises(UserError):
            ordered_parallel(list(range(10)), operation, lambda *a: None, cancelled)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(read_json(self.root / 'completed.json'), {'complete': True})
        self.assertLessEqual(len(seen), 2)

    def test_chunk_seam_can_join_a_split_thought_without_overlapping_ids(self):
        left = {'source_ids': [1,2], 'title': '师承', 'detail': '张仲景跟随老师学医，每次出门。'}
        right = {'source_ids': [3,4], 'title': '随诊', 'detail': '他跟在叔叔后面学习望闻问切，强调勤学与实践。'}
        class Client:
            calls = 0
            def complete(self, prompt, payload, validator):
                self.calls += 1
                value = {'merge': True, 'note': {'source_ids': [1,2,3,4], 'title': '师承与实践',
                         'detail': '张仲景跟随老师学医，每次出门跟在叔叔后面学习望闻问切，强调勤学与实践。'}}
                validator(value)
                return value
        client = Client()
        self.assertEqual(stitch_notes([left,right], client, lambda *a: None), [left,right])
        self.assertEqual(client.calls, 0)
        result = stitch_notes([left,right], client, lambda *a: None, seams={1})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['source_ids'], [1,2,3,4])
        self.assertIn('望闻问切', result[0]['detail'])

    def test_overview_references_inherit_full_lineage_instead_of_guessing_caption_ids(self):
        notes = [{'source_ids': [10,11,12], 'title': '早段', 'detail': '前提'},
                 {'source_ids': [900,901], 'title': '后段', 'detail': '后文更正'}]
        self.assertEqual([v['note_id'] for v in outline_items(notes)], [0,1])
        result = expand_note({'note_ids': [0,1], 'text': '保留前提与后文修正'}, dict(enumerate(notes)))
        self.assertEqual(result['source_ids'], [10,11,12,900,901])
        with self.assertRaises(ValueError):
            expand_note({'note_ids': [900], 'text': '错把原稿编号作为笔记编号'}, dict(enumerate(notes)))

    def test_hierarchical_summary_keeps_lineage_through_multiple_compressions(self):
        FakeClient.long_notes = True
        values = segments(300)
        with patch('summary.ChatClient', FakeClient):
            result, _ = summarize(values, 'long', {'model': 'fake'}, self.root, lambda *a: None)
        self.assertEqual(result['key_points'][0]['source_ids'], list(range(300)))
        self.assertTrue(any(prompt == REDUCE_PROMPT for prompt, _ in FakeClient.seen))

    def test_model_lookup_uses_manager_and_does_not_implicitly_download(self):
        from media import prepare_model
        with patch('asr_models.acquire_model', return_value=contextlib.nullcontext({'paths': {'asr': '/managed/turbo'}})) as acquire:
            self.assertEqual(prepare_model('turbo', self.root, lambda *a: None), '/managed/turbo')
            self.assertEqual(acquire.call_args.args, (self.root, 'turbo'))

    def test_missing_model_is_not_converted_or_downloaded_from_transcription(self):
        from media import prepare_model
        with patch('asr_models.acquire_model', side_effect=UserError('请下载')):
            with self.assertRaisesRegex(UserError, '请下载'):
                prepare_model('turbo', self.root, lambda *a: None)

    def test_nonfinite_times_and_empty_speech_are_rejected(self):
        for values in ([], [{'id': 0, 'start': float('nan'), 'end': 2, 'text': 'x'}],
                       [{'id': 0, 'start': 2, 'end': 1, 'text': 'x'}]):
            with self.assertRaises(UserError): validate_segments(values)

    def test_ninety_minute_chunking_is_lossless_and_bounded(self):
        values = segments(2700)
        for s in values: s['start'], s['end'] = s['id'] * 2., s['id'] * 2. + 1.9
        groups = batches(values, 4500)
        self.assertEqual([s for g in groups for s in g], values)
        self.assertTrue(all(sum(len(encoded(s).encode()) for s in g) <= 4500 for g in groups))

    def test_adaptive_split_keeps_coverage_and_boundary_context(self):
        FakeClient.split_limit = 2
        values = segments(9)
        with patch('summary.ChatClient', FakeClient):
            result, condensed = summarize(values, 'test', {'model': 'fake'}, self.root, lambda *a: None)
        self.assertEqual([i for v in condensed for i in v['source_ids']], list(range(9)))
        calls = [p for prompt, p in FakeClient.seen if prompt == CHUNK_PROMPT and p['segments'][0]['id'] > 0]
        self.assertTrue(any(p['context_before'] and p['context_after'] for p in calls))
        self.assertEqual(result['tokensThisRun'], 99)

    def test_single_huge_caption_keeps_all_text_and_original_id(self):
        values = [{'id': 0, 'start': 0, 'end': 100, 'text': '中文🧪attention ' * 1800}]
        with patch('summary.ChatClient', FakeClient):
            _, condensed = summarize(values, 'test', {'model': 'fake'}, self.root, lambda *a: None)
        self.assertEqual(''.join(v['text'] for v in condensed), values[0]['text'])
        self.assertTrue(all(v['source_ids'] == [0] for v in condensed))

    def test_hierarchical_overview_retains_all_detailed_notes(self):
        FakeClient.long_notes = True
        values = segments(1000)
        with patch('summary.ChatClient', FakeClient):
            result, condensed = summarize(values, 'test', {'model': 'fake'}, self.root, lambda *a: None)
        self.assertTrue(any(prompt == REDUCE_PROMPT for prompt, _ in FakeClient.seen))
        self.assertEqual(len(condensed), len(values))
        self.assertEqual(result['chapters'], read_json(self.root / 'chapters.json'))
        self.assertGreater(len(encoded(result['chapters']).encode()), 18000)

    def test_model_cannot_cite_nonexistent_sources(self):
        with self.assertRaises(ValueError):
            validate_overview({'overview': 'ok', 'key_points': [{'source_ids': [99], 'text': 'bad'}], 'uncertainties': []}, {0})

    def test_overlapping_asr_windows_have_single_word_ownership(self):
        first = {'segments': [{'start': 238., 'end': 241., 'text': '上下文', 'words': [
            {'start': 238., 'end': 239.5, 'word': '上'}, {'start': 239.5, 'end': 240.5, 'word': '下'}, {'start': 240.5, 'end': 241., 'word': '文'}]}]}
        second = copy.deepcopy(first)
        for seg in second['segments']:
            seg['start'] -= 232; seg['end'] -= 232
            for word in seg['words']: word['start'] -= 232; word['end'] -= 232
        a = owned_words(first, 0, 0, 240)
        b = owned_words(second, 232, 240, 480)
        self.assertEqual(''.join(x['text'] for x in a + b), '上下文')
        self.assertTrue(all(0 <= s['start'] <= s['end'] for s in a + b))

    def test_asr_numeric_scalars_are_normalized_for_persistence(self):
        class TimingScalar(float):
            pass
        result = {'segments': [{'start': TimingScalar(0.2), 'end': TimingScalar(2.0), 'text': '中文',
                                'words': [{'start': TimingScalar(0.2), 'end': TimingScalar(2.0), 'word': '中文'}]}]}
        values = owned_words(result, 0, 0, 240)
        self.assertIs(type(values[0]['start']), float)
        self.assertIs(type(values[0]['end']), float)
        validate_segments([{'id': 0, **values[0]}])

    def test_private_atomic_storage_and_originals_are_independent(self):
        values = segments()
        save_transcript(self.root, values)
        write_text(self.root / 'study-notes.md', 'model summary')
        self.assertEqual(read_json(self.root / 'transcript.json'), values)
        self.assertEqual((self.root / 'transcript.json').stat().st_mode & 0o777, 0o600)
        write_json(self.root / 'transcript.json', values)
        self.assertFalse(list(self.root.glob('.writing-*')))

    def test_cleanup_is_scoped_and_does_not_follow_symlink(self):
        job = self.root / 'job'; outside = self.root / 'outside'
        job.mkdir(); outside.mkdir()
        write_text(outside / 'keep', 'important')
        (job / 'media').symlink_to(outside, target_is_directory=True)
        write_text(job / 'temporary' / 'audio.wav', 'temporary')
        write_text(job / 'transcript.txt', 'keep text')
        clean_media(job)
        self.assertTrue((outside / 'keep').exists())
        self.assertTrue((job / 'transcript.txt').exists())
        self.assertFalse((job / 'media').exists())
        self.assertFalse((job / 'temporary').exists())

    def make_job(self, keep):
        folder = self.root / 'task-1'
        folder.mkdir()
        request = {'title': 'sample', 'url': 'https://www.bilibili.com/video/BV1Ywn46EESf/', 'source': '字幕',
                   'useWhisper': False, 'mediaMode': 'video', 'keepMedia': keep, 'segments': segments(2)}
        write_json(folder / 'request.json', request)
        write_json(folder / 'job.json', {'id': folder.name, 'state': 'pending'})
        write_text(folder / 'media' / 'source.mp4', 'media')
        return folder

    def test_api_failure_keeps_original_and_media_then_resume_cleans_after_success(self):
        folder = self.make_job(False)
        with patch('worker.download', return_value=folder / 'media/source.mp4'), patch('worker.summarize', side_effect=UserError('API unavailable')):
            self.assertEqual(run(folder, self.root), 2)
        self.assertEqual(read_json(folder / 'transcript.json'), segments(2))
        self.assertTrue((folder / 'media/source.mp4').exists())
        notes = {'overview': 'ok', 'chapters': [], 'key_points': [], 'uncertainties': [], 'model': 'fake'}
        with patch('worker.download', return_value=folder / 'media/source.mp4'), patch('worker.summarize', return_value=(notes, [])):
            self.assertEqual(run(folder, self.root), 0)
        self.assertTrue((folder / 'study-notes.md').exists())
        self.assertTrue((folder / 'condensed.md').exists())
        self.assertFalse((folder / 'media').exists())
        self.assertEqual(read_json(folder / 'job.json')['state'], 'completed')

    def test_keep_media_option_retains_file_after_completion(self):
        folder = self.make_job(True)
        notes = {'overview': 'ok', 'chapters': [], 'key_points': [], 'uncertainties': [], 'model': 'fake'}
        with patch('worker.download', return_value=folder / 'media/source.mp4'), patch('worker.summarize', return_value=(notes, [])):
            self.assertEqual(run(folder, self.root), 0)
        self.assertEqual(read_json(folder / 'job.json')['mediaFiles'], ['media/source.mp4'])
        self.assertTrue((folder / 'media/source.mp4').is_file())

    def test_cancelled_job_keeps_resume_material(self):
        folder = self.make_job(False)
        with patch('worker.download', side_effect=KeyboardInterrupt):
            self.assertEqual(run(folder, self.root), 2)
        self.assertEqual(read_json(folder / 'job.json')['state'], 'cancelled')
        self.assertTrue((folder / 'media/source.mp4').is_file())

    def test_truncated_reply_is_never_cached(self):
        client = ChatClient({'baseURL': 'https://example.com/v1', 'model': 'fake', 'apiKey': 'test-only'}, self.root, lambda *a: None)
        with patch.object(client, '_request', return_value={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}):
            with self.assertRaises(OutputLimit): client.complete('prompt', {}, lambda x: None)
        self.assertFalse(list(self.root.glob('*.json')))

    def test_cache_is_validated_and_model_settings_change_identity(self):
        config = {'baseURL': 'https://example.com/v1', 'model': 'fake', 'apiKey': 'test-only'}
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
        validator = lambda value: self.assertIs(value['ok'], True)
        client = ChatClient(config, self.root, lambda *a: None)
        with patch.object(client, '_request', return_value=response) as call:
            client.complete('prompt', {}, validator)
            client.complete('prompt', {}, validator)
            self.assertEqual(call.call_count, 1)
        client = ChatClient({**config, 'model': 'different'}, self.root, lambda *a: None)
        with patch.object(client, '_request', return_value=response) as call:
            client.complete('prompt', {}, validator)
            self.assertEqual(call.call_count, 1)
        self.assertNotIn('test-only', ''.join(p.read_text() for p in self.root.iterdir()))


if __name__ == '__main__':
    unittest.main()
