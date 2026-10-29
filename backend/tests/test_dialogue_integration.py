"""Cross-layer contracts; synthetic text only, no paid API or private data."""
import copy
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

from common import UserError, read_json, save_transcript, write_json
from recording import export_dialogue, run_recording, SPEAKERS
from worker import validate_request


class DialogueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / str(uuid.uuid4())
        self.folder.mkdir()
        self.segments = [
            {"id": 0, "text": "先测试，", "start": 0., "end": 1., "speaker": "A"},
            {"id": 1, "text": "再", "start": 1., "end": 1.2, "speaker": "unknown"},
            {"id": 2, "text": "决定。", "start": 1.2, "end": 2., "speaker": "A"}]
        save_transcript(self.folder, self.segments)
        self.request = {"sourceKind": "recording", "recordingAction": "summarize", "title": "条件保留测试"}
        self.record = {"id": self.folder.name, "title": self.request["title"]}
        self.notes = {"model": "test", "overview": "决定以前需测试。", "chapters": [], "key_points": [], "uncertainties": []}
        self.condensed = [{"id": "r0", "source_ids": [0, 1, 2], "text": "先测试，再决定。", "parts": [
            {"id": "p0", "source_ids": [0], "speaker": "A", "text": "先测试，"},
            {"id": "p1", "source_ids": [1], "speaker": "unknown", "text": "再"},
            {"id": "p2", "source_ids": [2], "speaker": "A", "text": "决定。"}]}]

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_request_is_not_silently_switched(self):
        write_json(self.folder / "request.json", self.request)
        with patch("summary_v2.summarize", return_value=(self.notes, self.condensed)) as legacy:
            run_recording(self.request, self.record, self.folder, self.root, lambda *a: None, lambda *a: {})
        self.assertEqual(legacy.call_count, 1)
        self.assertEqual(read_json(self.folder / "job.json")["dialogueStrategy"], "legacy")

    def test_mixed_parts_export_rename_and_raw_transcript_are_independent(self):
        export_dialogue(self.folder, "测试", self.segments, {**SPEAKERS, "A": "甲"}, self.notes, self.condensed)
        cleaned = (self.folder / "condensed.md").read_text()
        self.assertIn("[甲] 先测试， [归属未确定] 再 [甲] 决定。", cleaned)
        self.assertNotIn("未确定：先测试", cleaned)
        self.assertEqual(read_json(self.folder / "transcript.json"), self.segments)

    def test_new_route_preserves_settings_identity_and_blocks_stale_publication(self):
        request = {**self.request, "dialogueStrategy": "context-v2"}
        write_json(self.folder / "request.json", request)
        received = []
        def summarize(segments, title, config, folder, emit):
            received.append(config)
            self.assertEqual(config["reasoningEffort"], "high")
            self.assertEqual(config["pricing"], {"input": 2})
            self.assertEqual(config["sourceKind"], "recording")
            self.assertTrue(config["transcriptIdentity"])
            config["beforePublish"]()
            changed = copy.deepcopy(segments)
            changed[0]["text"] = "不测试，"
            save_transcript(folder, changed)
            with self.assertRaisesRegex(UserError, "未覆盖"):
                config["beforePublish"]()
            save_transcript(folder, segments)
            return self.notes, self.condensed
        with patch.dict("sys.modules", {"dialogue_reader": types.SimpleNamespace(summarize=summarize)}):
            run_recording(request, self.record, self.folder, self.root, lambda *a: None,
                          lambda *a: {"reasoningEffort": "high", "pricing": {"input": 2}})
        self.assertEqual(len(received), 1)
        self.assertEqual(read_json(self.folder / "job.json")["dialogueStrategy"], "context-v2")

    def test_unknown_strategy_cannot_fall_back_to_legacy(self):
        for strategy in ("new-future", ["context-v2"]):
            with self.assertRaises(UserError):
                validate_request({**self.request, "dialogueStrategy": strategy})
        for strategy in (None, "legacy", "context-v2"):
            validate_request({**self.request, "dialogueStrategy": strategy})

    def test_meeting_routes_share_settings_publication_guard_and_action_export(self):
        for strategy in ("meeting-v3", "meeting-baseline"):
            request = {**self.request, "dialogueStrategy": strategy}
            write_json(self.folder / "request.json", request)
            calls = []
            notes = {**self.notes, "action_items": [{"text": "[A]测试通过后再发送。", "source_ids": [0, 1, 2]}]}
            def summarize(segments, title, config, folder, emit):
                calls.append(config)
                self.assertEqual(config["reasoningEffort"], "high")
                self.assertEqual(config["pricing"], {"inputPerMillion": 2})
                self.assertEqual(config["sourceKind"], "recording")
                self.assertEqual(config["dialogueStrategy"], strategy)
                config["beforePublish"]()
                return notes, self.condensed
            with patch.dict("sys.modules", {"meeting_reader": types.SimpleNamespace(summarize=summarize)}):
                run_recording(request, self.record, self.folder, self.root, lambda *a: None,
                              lambda *a: {"reasoningEffort": "high", "pricing": {"inputPerMillion": 2}})
            self.assertEqual(len(calls), 1)
            self.assertEqual(read_json(self.folder / "job.json")["dialogueStrategy"], strategy)
            self.assertIn("明确行动与前提", (self.folder / "study-notes.md").read_text())
            self.assertIn("测试通过后再发送", (self.folder / "study-notes.md").read_text())
            self.assertEqual(read_json(self.folder / "transcript.json"), self.segments)


if __name__ == "__main__":
    unittest.main()
