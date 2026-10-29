import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import UserError, read_json, write_json
from dialogue_context import build_context
from meeting_contract import validate_item, validate_document, punctuation_rows, render_item
from meeting_reader import MeetingReader
from summary import ChatClient, InputContextLimit, ReasoningOutputExhausted
from worker import validate_request


def transcript():
    return [
        {"id": 0, "start": 0., "end": 3., "text": "中文资料已经有了。实验条件也齐备。", "speaker": "B"},
        {"id": 1, "start": 3., "end": 5., "text": "先复现程序，测试通过才发送结果。", "speaker": "A"},
        {"id": 2, "start": 5., "end": 6., "text": "不", "speaker": "unknown"},
        {"id": 3, "start": 6., "end": 7., "text": "费用2.05元。", "speaker": "B"}]


def item(unit, quote=None, **changes):
    p = unit["parts"][0]
    result = {"text": "资料已经具备。", "kind": "fact", "state": "current", "conditions": [], "scope": None,
              "speaker": p["speaker"], "attribution_parts": [p["id"]] if p["speaker"] in ("A", "B") else [],
              "owner": None, "due": None, "evidence": [{"unit": unit["id"], "quote": quote or p["text"]}]}
    result.update(changes)
    return result


def document(units):
    return {"overview": {"text": "资料已具备，后续执行附有条件。", "u": [units[0]["id"]]},
            "chapters": [{"title": "资源与安排", "claims": [item(units[0])]}], "action_items": [], "uncertainties": []}


def empty_review(checks=(), targets=()):
    return {"patches": [], "additions": [], "issues": [], "unhandled_errors": [],
            "assessments": [{"check_id": cid, "status": "covered", "target_ids": [next(iter(targets))], "reason": "合成样例直接保留该事实"} for cid in checks]}


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.config = {"baseURL": "https://example.com/v1", "model": "fixture", "apiKey": "test-only", "reasoningEffort": "high", "dialogueMaxOutputTokens": 131072}
        self.requests, self.hook = [], None

    def reader(self, segments=None, **config):
        self.engine = MeetingReader(segments or transcript(), "合成录音", {**self.config, **config}, self.folder, lambda *a: None)
        return self.engine

    def response(self, client, body, phase):
        client.request_count += 1
        payload = json.loads(body["messages"][1]["content"])
        self.requests.append((phase, payload, body))
        if self.hook:
            result = self.hook(client, body, phase, payload)
            if result is not None:
                return result
        ids = [int(i) for i in re.findall(r"\[u(\d+)\]", payload.get("sources", ""))]
        units = [u for u in self.engine.units if u["id"] in ids]
        if phase in ("meeting_draft", "meeting_merge"):
            value = document(units or self.engine.units)
        elif phase == "meeting_evidence":
            value = {"items": [item(u) for u in units], "coverage": [{"unit": i, "disposition": "substantive"} for i in payload["owned_units"]]}
        elif phase == "meeting_review":
            value = empty_review(payload["owned_checks"], payload["owned_targets"])
        elif phase == "meeting_reread":
            value = {**empty_review(), "status": "unresolved", "reason": "没有新增证据"}
        elif phase == "meeting_punctuation":
            value = {"rows": [{"first": payload["owned_parts"][0], "last": payload["owned_parts"][-1]}], "insertions": []}
        else:
            raise AssertionError(phase)
        return self.envelope(value)

    def envelope(self, value):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180}}

    def run_reader(self, **config):
        engine = self.reader(**config)
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            result = engine.run()
        return engine, result

    def prepared(self):
        e = self.reader()
        d = document(e.units); validate_document(d, e.units); e.identify(d)
        c = item(e.units[0]); validate_item(c, e.lookup); c["id"] = "check-fixture"
        e.checks[c["id"]] = c
        return e

    def test_source_inventory_is_independent_and_completed_replay_makes_no_request(self):
        engine, (notes, rows) = self.run_reader()
        inventory = next(p for phase, p, _ in self.requests if phase == "meeting_evidence")
        self.assertEqual(set(inventory), {"title", "sources", "owned_units", "context"})
        self.assertNotIn("targets", inventory)
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft", "meeting_evidence", "meeting_review", "meeting_punctuation"])
        self.assertEqual("".join(r["text"] for r in rows), "".join(s["text"] for s in transcript()))
        self.assertEqual(notes["assessmentCount"], 1)
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["schemaVersion"], 2)
        before = len(self.requests)
        _, (again, _) = self.run_reader()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(again["chapters"], notes["chapters"])
        self.assertEqual(again["tokensThisRun"], 0)

    def test_baseline_does_not_review_but_uses_same_draft_and_readable_dialogue(self):
        _, (notes, _) = self.run_reader(dialogueStrategy="meeting-baseline")
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft", "meeting_punctuation"])
        self.assertEqual(notes["summaryStrategy"], "meeting-baseline")
        self.assertEqual(notes["reviewedParagraphs"], 0)

    def test_identity_isolates_strategy_effort_source_and_limits(self):
        original = self.reader().identity
        for opts in ({"dialogueStrategy": "meeting-baseline"}, {"reasoningEffort": "low"}, {"dialogueMaxRereads": 0}, {"sourceRevision": "changed"}):
            self.assertNotEqual(self.reader(**opts).identity, original)
        self.assertEqual(self.reader(beforePublish=lambda: None).identity, original)

    def test_quotes_reject_rewritten_terms_and_unknown_speaker_promotion(self):
        e = self.reader(); claim = item(e.units[0])
        bad = copy.deepcopy(claim); bad["evidence"][0]["quote"] = "中文材料准备好了。"
        with self.assertRaises(ValueError): validate_item(bad, e.lookup)
        bad = copy.deepcopy(claim); bad["speaker"] = "A"
        with self.assertRaises(ValueError): validate_item(bad, e.lookup)
        bad = item(e.units[0], "不", speaker="A", attribution_parts=[2])
        with self.assertRaises(ValueError): validate_item(bad, e.lookup)

    def test_rendering_keeps_condition_scope_state_and_action_deadline_separate(self):
        e = self.reader()
        claim = item(e.units[0], kind="action", state="pending", text="发送结果", conditions=["测试通过"], scope="本项目", owner="A", due=None)
        validate_item(claim, e.lookup, action=True)
        output = render_item(claim)
        self.assertIn("待完成", output); self.assertIn("前提：测试通过", output)
        self.assertIn("适用范围：本项目", output); self.assertNotIn("期限", output)

    def test_punctuation_preserves_characters_and_rejects_decimal_or_word_splitting(self):
        units = build_context(transcript()); parts = units[0]["parts"]
        value = {"rows": [{"first": parts[0]["id"], "last": parts[-1]["id"]}], "insertions": [{"part_id": 2, "offset": 1, "text": "。"}]}
        rows = punctuation_rows(value, units)
        self.assertEqual(rows[0]["parts"][2]["text"], "不。")
        for insert in ({"part_id": 2, "offset": 1, "text": "可以"}, {"part_id": 3, "offset": 3, "text": "，"}):
            with self.assertRaises(ValueError): punctuation_rows({**value, "insertions": [insert]}, units)
        split = build_context([{**transcript()[0], "text": "2", "speaker": "A"}, {**transcript()[1], "text": ".05", "speaker": "unknown"}])
        with self.assertRaises(ValueError):
            punctuation_rows({"rows": [{"first": 0, "last": 1}], "insertions": [{"part_id": 0, "offset": 1, "text": "，"}]}, split)

    def test_rows_cannot_skip_short_answer_or_reorder_ownership(self):
        units = build_context(transcript())
        for rows in ([{"first": 0, "last": 1}, {"first": 3, "last": 3}], [{"first": 1, "last": 3}, {"first": 0, "last": 0}]):
            with self.assertRaises(ValueError): punctuation_rows({"rows": rows, "insertions": []}, units)

    def test_issue_identity_ignores_paraphrase_but_keeps_distinct_fields(self):
        e = self.prepared(); proposal = empty_review()
        issue = {"target_id": "claim-0", "field": "state", "kind": "state", "question": "资料现在是否已有？", "u": [0], "query": "资料", "status": "unresolved"}
        proposal["issues"] = [issue]; e.apply_repair(proposal, "review")
        proposal["issues"] = [{**issue, "question": "材料是否还要准备？", "query": "中文"}]; e.apply_repair(proposal, "review")
        self.assertEqual(len(e.issues), 1); self.assertEqual(e.duplicate_issues, 1)
        proposal["issues"] = [{**issue, "field": "scope"}]; e.apply_repair(proposal, "review")
        self.assertEqual(len(e.issues), 2)

    def test_coverage_cannot_claim_success_without_target_and_readback_cannot_spawn_issues(self):
        e = self.prepared(); targets = e.meeting_targets()
        value = empty_review(e.checks, targets); value["assessments"][0]["target_ids"] = []
        with self.assertRaises(ValueError): e.validate_repair(value, e.units, targets, set(targets), e.checks)
        value = {**empty_review(), "status": "unresolved", "reason": "没有证据", "issues": [{}]}
        with self.assertRaises(ValueError): e.validate_repair(value, e.units, targets, set(targets), {}, reread=True)

    def test_readback_once_no_progress_and_query_range_cache(self):
        e = self.prepared(); proposal = empty_review()
        proposal["issues"] = [{"target_id": "claim-0", "field": "state", "kind": "state", "question": "有无准备？", "u": [0], "query": "资料", "status": "unresolved"}]
        e.apply_repair(proposal, "review")
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            e.reread(); e.reread()
        self.assertEqual(e.reread_count, 1); self.assertEqual(e.no_progress, 1)
        e.read_sources([0], "中文"); self.assertGreater(e.retrieval_hits, 0)

    def test_needs_audio_never_automatically_rereads(self):
        e = self.prepared(); p = empty_review()
        p["issues"] = [{"target_id": "claim-0", "field": "text", "kind": "audio", "question": "回听", "u": [0], "query": "", "status": "needs_audio"}]
        e.apply_repair(p, "review"); e.reread(); self.assertEqual(e.reread_count, 0)

    def test_cancel_after_independent_evidence_resumes_successful_stages(self):
        def stop(client, body, phase, payload):
            if phase == "meeting_review": raise KeyboardInterrupt()
        self.hook = stop
        with self.assertRaises(KeyboardInterrupt): self.run_reader()
        self.assertFalse((self.folder / "dialogue-results.json").exists())
        previous = len(self.requests); self.hook = None
        _, _ = self.run_reader()
        self.assertEqual([p for p, _, _ in self.requests[previous:]], ["meeting_review", "meeting_punctuation"])

    def test_stale_publication_preserves_old_results_and_checkpoint_success(self):
        saved = {"old": "preserved"}; write_json(self.folder / "dialogue-results.json", saved)
        def stale(): raise UserError("source changed")
        with self.assertRaises(UserError): self.run_reader(beforePublish=stale)
        self.assertEqual(read_json(self.folder / "dialogue-results.json"), saved)
        self.assertTrue(list((self.folder / "checkpoints/meeting-v3").glob("*/*.json")))

    def test_pure_reasoning_exhaustion_does_not_fragment_source(self):
        def exhaust(client, body, phase, payload):
            if phase == "meeting_draft": return {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "not retained"}}], "usage": {"completion_tokens": 32768}}
        self.hook = exhaust
        with self.assertRaises(ReasoningOutputExhausted): self.run_reader()
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft", "meeting_draft"])
        self.assertFalse((self.folder / "dialogue-results.json").exists())

    def test_worker_accepts_new_strategies_and_rejects_unknown(self):
        for strategy in ("meeting-v3", "meeting-baseline", "context-v2", "legacy"):
            validate_request({"sourceKind": "recording", "title": "测试", "dialogueStrategy": strategy})
        with self.assertRaises(UserError): validate_request({"sourceKind": "recording", "title": "测试", "dialogueStrategy": "typo"})


if __name__ == "__main__":
    unittest.main()
