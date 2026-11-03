import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import UserError, read_json
from meeting_reader import MeetingReader, compact_evidence, format_dialogue
from meeting_understanding import normalize_context
from summary import ChatClient, InputContextLimit
from test_meeting_reader import transcript, document, item
from worker import validate_request


class UnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.config = {"baseURL": "https://example.com/v1", "model": "fixture", "apiKey": "test-only",
                       "reasoningEffort": "high", "dialogueStrategy": "meeting-v4", "recordingContext": "我负责实现，关注资料状态与执行前提。"}
        self.requests = []
        self.fail_phase = None

    def engine(self, **changes):
        self.reader = MeetingReader(transcript(), "合成讨论", {**self.config, **changes}, self.folder, lambda *a: None)
        return self.reader

    def response(self, client, body, phase):
        payload = json.loads(body["messages"][1]["content"])
        self.requests.append((phase, payload, body))
        if phase == self.fail_phase:
            raise KeyboardInterrupt()
        client.request_count += 1
        ids = set(int(i) for i in re.findall(r"\[u(\d+)\]", payload.get("sources", "")))
        units = [u for u in self.reader.units if u["id"] in ids] or self.reader.units
        if phase in ("meeting_draft", "meeting_merge"):
            value = document(units)
        elif phase == "meeting_evidence":
            value = {"items": [{"text": u["text"], "evidence": item(u)["evidence"]} for u in units]}
        elif phase == "meeting_review":
            value = {"patches": [], "additions": [], "issues": []}
        else:
            raise AssertionError(phase)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(compact_evidence(value), ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180}}

    def run_engine(self, **changes):
        engine = self.engine(**changes)
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            return engine.run()

    def test_three_stages_use_same_background_full_source_and_no_draft_in_map(self):
        notes, rows = self.run_engine()
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_evidence", "meeting_draft", "meeting_review"])
        first, second, review = [payload for _, payload, _ in self.requests]
        self.assertNotIn("targets", first)
        self.assertNotIn("information_notes", first)
        self.assertTrue(second["information_notes"])
        self.assertEqual(first["sources"], second["sources"])
        self.assertEqual(first["sources"], review["sources"])
        self.assertTrue(all(p["task_context"] == {"origin": "user", "text": self.config["recordingContext"]} for _, p, _ in self.requests))
        self.assertEqual(notes["recordingContext"], self.config["recordingContext"])
        self.assertEqual(notes["summaryStrategy"], "meeting-v4")
        self.assertEqual("".join(x["text"] for x in rows), "".join(x["text"] for x in transcript()))
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["strategy"], "meeting-v4")

    def test_old_workflow_keeps_order_and_gets_identical_background(self):
        self.run_engine(dialogueStrategy="meeting-v3")
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft", "meeting_evidence", "meeting_review"])
        self.assertTrue(all(p["task_context"]["text"] == self.config["recordingContext"] for _, p, _ in self.requests))
        self.assertNotIn("information_notes", self.requests[0][1])

    def test_resume_reuses_completed_map_after_interruption(self):
        self.fail_phase = "meeting_draft"
        with self.assertRaises(KeyboardInterrupt): self.run_engine()
        self.assertTrue((self.folder / "meeting-evidence.json").exists())
        self.assertFalse((self.folder / "dialogue-results.json").exists())
        self.fail_phase = None; self.requests = []
        self.run_engine()
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft", "meeting_review"])
        self.assertEqual(self.reader.client.cache_hits, 1)
        self.requests = []
        self.run_engine()
        self.assertEqual(self.requests, [])

    def test_context_and_strategy_invalidate_checkpoints_and_context_is_budgeted(self):
        self.run_engine(); original = self.reader.identity
        self.requests = []
        self.run_engine(recordingContext="关注不同的执行前提。")
        self.assertEqual(len(self.requests), 3)
        self.assertNotEqual(self.reader.identity, original)
        self.assertNotEqual(self.engine(reasoningEffort="low").identity, original)
        self.assertNotEqual(self.engine(dialogueStrategy="meeting-v3").identity, original)
        without = self.engine(recordingContext=None).identity
        self.assertEqual(without, self.engine(recordingContext="   ").identity)
        e = self.engine(recordingContext="长" * 4000, dialogueInputTokens=6000)
        # Direct call checks the entire augmented request, never just sources.
        with patch.object(ChatClient, "_request", side_effect=AssertionError("Must fail before a request")):
            e.input_budget = 1000
            with self.assertRaisesRegex(UserError, "超过输入容量"): e.draft(e.units)

    def test_changed_background_cannot_format_or_publish_over_existing_result(self):
        self.run_engine(); previous = (self.folder / "dialogue-results.json").read_bytes()
        with self.assertRaises(UserError):
            format_dialogue(transcript(), "合成讨论", {**self.config, "recordingContext": "不同背景"}, self.folder, lambda *a: None)
        self.assertEqual((self.folder / "dialogue-results.json").read_bytes(), previous)
        def changed(): raise UserError("背景已变化")
        with self.assertRaises(UserError): self.run_engine(recordingContext="新背景", beforePublish=changed)
        self.assertEqual((self.folder / "dialogue-results.json").read_bytes(), previous)

    def test_context_validation_and_supported_strategies(self):
        self.assertEqual(normalize_context("  背景\n"), "背景")
        for value in ([], 3, "长" * 4001):
            with self.assertRaises(UserError): normalize_context(value)
        request = {"sourceKind": "recording", "title": "合成录音", "recordingAction": "summarize",
                   "diarizationMode": "two", "recordingContext": "用户背景"}
        for strategy in ("meeting-v3", "meeting-v4", "meeting-baseline"):
            validate_request({**request, "dialogueStrategy": strategy})
        for strategy in ("legacy", "context-v2"):
            with self.assertRaises(UserError): validate_request({**request, "dialogueStrategy": strategy})
