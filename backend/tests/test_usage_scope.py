import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from common import read_json
from summary import ChatClient
from usage import UsageLedger


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, _):
        return json.dumps(self.value).encode()


class UsageScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"baseURL": "https://example.com/v1", "model": "compatible-model", "apiKey": "test-secret",
                       "usageRoot": str(self.root), "taskID": "test-task", "taskTitle": "对话测试",
                       "sourceKind": "recording", "reasoningEffort": "high"}

    def tearDown(self):
        self.temp.cleanup()

    def events(self):
        return [read_json(path) for path in (self.root / "usage/events").glob("*.json")]

    def test_attempt_persists_source_and_requested_effort_before_interruption(self):
        ledger = UsageLedger(self.config)
        event = ledger.begin("whole")
        saved = self.events()[0]
        self.assertEqual(saved["sourceKind"], "recording")
        self.assertEqual(saved["reasoningEffort"], "high")
        self.assertEqual(saved["status"], "in_flight")
        self.config.update(sourceKind="bilibili", reasoningEffort="low")
        ledger.finish(event, "cancelled")
        saved = self.events()[0]
        self.assertEqual(saved["sourceKind"], "recording")
        self.assertEqual(saved["reasoningEffort"], "high")
        self.assertEqual(saved["status"], "cancelled")
        self.assertNotIn("inputTokens", saved["usage"])

    def test_missing_source_defaults_only_associated_task_calls_to_video(self):
        self.config.pop("sourceKind")
        self.config.pop("reasoningEffort")
        event = UsageLedger(self.config).begin("whole")
        self.assertEqual(event["sourceKind"], "bilibili")
        self.assertEqual(event["reasoningEffort"], "low")
        self.config.pop("taskID")
        event = UsageLedger(self.config).begin("whole")
        self.assertNotIn("sourceKind", event)

    def test_unknown_source_and_unvalidated_effort_do_not_copy_arbitrary_content(self):
        self.config.update(sourceKind="private-arbitrary-metadata", reasoningEffort="invalid-secret")
        event = UsageLedger(self.config).begin("whole")
        self.assertEqual(event["sourceKind"], "unknown")
        self.assertNotIn("reasoningEffort", event)
        contents = json.dumps(self.events())
        self.assertNotIn("private-arbitrary-metadata", contents)
        self.assertNotIn("invalid-secret", contents)
        self.assertNotIn("test-secret", contents)

    def test_recording_request_and_both_ledgers_agree_and_cache_does_not_rebill(self):
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}
        opener = Mock(); opener.open.return_value = Response(response)
        client = ChatClient(self.config, self.root / "task-cache", lambda *args: None)
        with patch("summary.urllib.request.build_opener", return_value=opener):
            for _ in range(2):
                self.assertEqual(client.complete("test", {}, lambda value: self.assertTrue(value["ok"]), phase="whole"), {"ok": True})
        self.assertEqual(opener.open.call_count, 1)
        sent = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(sent["reasoning_effort"], "high")
        self.assertEqual(len(self.events()), 1)
        event = self.events()[0]
        log = json.loads((self.root / "task-cache/usage.jsonl").read_text().strip())
        self.assertEqual(log["eventID"], event["id"])
        for row in (event, log):
            self.assertEqual(row["sourceKind"], "recording")
            self.assertEqual(row["reasoningEffort"], "high")
            self.assertEqual(row["phase"], "whole")
        self.assertEqual(event["usage"]["reportedTotalTokens"], 120)


if __name__ == "__main__":
    unittest.main()
