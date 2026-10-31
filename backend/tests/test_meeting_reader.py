import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import UserError, read_json, write_json
from dialogue_context import build_context
from dialogue_reader import Reader
from meeting_contract import validate_item, validate_evidence_item, validate_document, punctuation_rows, render_item, anchor, evidence_anchors, REREAD_PROMPT
from meeting_reader import MeetingReader, compact_evidence, format_dialogue
from summary import ChatClient, InputContextLimit, ReasoningOutputExhausted, ResponseOutputLimit
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
              "owner": None, "due": None, "evidence": [{"unit": unit["id"], "quote": quote} if quote is not None else
                                                       {"unit": unit["id"], "first_part": p["id"], "last_part": p["id"]}]}
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
            value = {"items": [{"text": u["text"], "evidence": item(u)["evidence"]} for u in units]}
        elif phase == "meeting_review":
            value = {"patches": [], "additions": [], "issues": [], "unhandled_errors": []}
        elif phase == "meeting_reread":
            value = {**empty_review(), "status": "unresolved", "reason": "没有新增证据"}
        elif phase == "meeting_punctuation":
            value = {"rows": [{"first": payload["owned_parts"][0], "last": payload["owned_parts"][-1]}], "insertions": []}
        else:
            raise AssertionError(phase)
        return self.envelope(compact_evidence(value))

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
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft", "meeting_evidence", "meeting_review"])
        self.assertEqual("".join(r["text"] for r in rows), "".join(s["text"] for s in transcript()))
        self.assertEqual(notes["assessmentCount"], 0)
        self.assertEqual(engine.review_responsibility[0]["checkIDs"], sorted(engine.checks))
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["formatting"]["status"], "not_requested")
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["schemaVersion"], 2)
        before = len(self.requests)
        _, (again, _) = self.run_reader()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(again["chapters"], notes["chapters"])
        self.assertEqual(again["tokensThisRun"], 0)

    def test_baseline_does_not_review_but_uses_same_draft_and_readable_dialogue(self):
        _, (notes, _) = self.run_reader(dialogueStrategy="meeting-baseline")
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_draft"])
        self.assertEqual(notes["summaryStrategy"], "meeting-baseline")
        self.assertEqual(notes["reviewedParagraphs"], 0)

    def test_sparse_statements_keep_exact_sources_and_actions_require_state(self):
        e = self.reader()
        sparse = {"text": "资料已经具备。", "evidence": item(e.units[0])["evidence"]}
        result = validate_item(copy.deepcopy(sparse), e.lookup)
        self.assertEqual(result["state"], "not_applicable")
        self.assertEqual(result["speaker"], "none")
        self.assertIsNone(result["owner"])
        self.assertEqual(render_item(result), sparse["text"])
        sent = compact_evidence(result)
        self.assertNotIn("owner", sent)
        self.assertNotIn("conditions", sent)
        self.assertEqual(validate_item(sent, e.lookup), result)
        with self.assertRaises(ValueError): validate_item(copy.deepcopy(sparse), e.lookup, action=True)
        with self.assertRaises(ValueError): validate_item({**copy.deepcopy(sparse), "kind": "proposal"}, e.lookup)
        action = validate_item({**copy.deepcopy(sparse), "state": "uncertain"}, e.lookup, action=True)
        self.assertEqual(action["kind"], "action")
        self.assertIsNone(action["due"])
        with self.assertRaises(ValueError):
            validate_item({**copy.deepcopy(sparse), "speaker": "A", "attribution_parts": [0]}, e.lookup)

    def test_full_source_review_doubt_stays_visible_without_repeating_same_context(self):
        def issue(client, body, phase, payload):
            if phase == "meeting_review":
                return self.envelope({"patches": [], "additions": [], "issues": [{"target_id": "claim-0",
                    "field": "scope", "kind": "scope", "question": "资料适用范围未确定。", "u": [0],
                    "query": "资料", "status": "unresolved"}]})
        self.hook = issue
        e, (notes, _) = self.run_reader()
        self.assertEqual(e.skipped_rereads, 1)
        self.assertEqual(e.reread_count, 0)
        self.assertEqual(len(notes["uncertainties"]), 1)
        self.assertEqual(notes["uncertainties"][0]["status"], "unresolved")
        self.assertEqual(next(iter(e.issue_context.values())), {u["id"] for u in e.units})
        self.assertNotIn("meeting_reread", [p for p, _, _ in self.requests])

    def test_partial_review_can_read_additional_distant_source_once(self):
        segments = [{"id": i, "start": i * 30., "end": i * 30. + 2,
                     "text": "尾部新证据。" if i == 3 else "资料已具备。", "speaker": "B"} for i in range(4)]
        e = self.reader(segments)
        d = document(e.units); validate_document(d, e.units); e.identify(d)
        p = empty_review()
        p["issues"] = [{"target_id": "claim-0", "field": "scope", "kind": "scope", "question": "资料的范围？",
                        "u": [0], "query": "尾部新证据", "status": "unresolved"}]
        e.apply_repair(p, "review")
        e.issue_context[next(iter(e.issues))] = {0, 1}
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            e.reread(); e.reread()
        self.assertEqual(e.reread_count, 1)
        self.assertIn("[u3]", self.requests[0][1]["sources"])
        self.assertEqual(e.skipped_rereads, 0)

    def format_saved(self, **config):
        value = {**self.config, "dialogueStrategy": "meeting-v3", "sourceRevision": self.engine.source_revision, **config}
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            return format_dialogue(transcript(), "合成录音", value, self.folder, lambda *a: None)

    def test_optional_formatting_only_calls_punctuation_and_preserves_notes_and_summary_diagnostics(self):
        _, (notes, _) = self.run_reader()
        summary_before = (self.folder / "summary-run.json").read_bytes()
        notes_before = (self.folder / "notes.json").read_bytes()
        self.requests.clear()
        formatted_notes, rows = self.format_saved()
        self.assertEqual([p for p, _, _ in self.requests], ["meeting_punctuation"])
        self.assertEqual(formatted_notes, notes)
        self.assertEqual((self.folder / "notes.json").read_bytes(), notes_before)
        self.assertEqual((self.folder / "summary-run.json").read_bytes(), summary_before)
        self.assertEqual("".join(r["text"] for r in rows), "".join(s["text"] for s in transcript()))
        bundle = read_json(self.folder / "dialogue-results.json")
        self.assertEqual(bundle["notes"], notes)
        self.assertEqual(bundle["formatting"]["status"], "completed")
        before = len(self.requests)
        self.format_saved()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(read_json(self.folder / "formatting-run.json")["checkpointHits"], 1)

    def test_formatting_failure_and_cancel_preserve_published_products(self):
        _, (notes, rows) = self.run_reader()
        self.requests.clear()
        def fail(client, body, phase, payload):
            if phase == "meeting_punctuation": raise UserError("合成网络故障")
        self.hook = fail
        self.assertEqual(self.format_saved(), (notes, rows))
        result = read_json(self.folder / "dialogue-results.json")
        self.assertEqual(result["formatting"]["status"], "failed")
        self.assertEqual(result["notes"], notes)
        self.assertEqual(result["condensed"], rows)
        def cancel(client, body, phase, payload):
            if phase == "meeting_punctuation": raise KeyboardInterrupt()
        self.hook = cancel
        with self.assertRaises(KeyboardInterrupt): self.format_saved()
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["formatting"]["status"], "cancelled")
        self.hook = None
        self.format_saved()
        self.assertTrue(all(p == "meeting_punctuation" for p, _, _ in self.requests))
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["notes"], notes)

    def test_formatting_rejects_stale_inputs_before_requests_and_changed_generation_at_publication(self):
        self.run_reader()
        saved = (self.folder / "dialogue-results.json").read_bytes()
        self.requests.clear()
        for config in ({"sourceRevision": "changed"}, {"recordingRevision": "changed"}, {"dialogueStrategy": "meeting-baseline"}):
            with self.assertRaises(UserError): self.format_saved(**config)
        self.assertEqual(self.requests, [])
        self.assertEqual((self.folder / "dialogue-results.json").read_bytes(), saved)
        def changed():
            raise UserError("原稿在处理中变化")
        with self.assertRaises(UserError): self.format_saved(beforePublish=changed)
        self.assertEqual((self.folder / "dialogue-results.json").read_bytes(), saved)
        replacement = {**read_json(self.folder / "dialogue-results.json"), "generationID": "another-generation"}
        def replaced(): write_json(self.folder / "dialogue-results.json", replacement)
        with self.assertRaises(UserError): self.format_saved(beforePublish=replaced)
        self.assertEqual(read_json(self.folder / "dialogue-results.json"), replacement)

    def test_formatting_cancel_resumes_only_unfinished_batch(self):
        segments = [{"id": i, "start": i * 30., "end": i * 30. + 2,
                     "text": "资料已具备。", "speaker": "B"} for i in range(2)]
        e = self.reader(segments)
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            e.run()
        notes = read_json(self.folder / "dialogue-results.json")["notes"]
        config = {**self.config, "dialogueStrategy": "meeting-v3", "sourceRevision": e.source_revision}
        self.requests.clear()
        def stop_second(client, body, phase, payload):
            if phase == "meeting_punctuation" and payload["owned_parts"] == [1]: raise KeyboardInterrupt()
        self.hook = stop_second
        with patch("meeting_reader.PART_BUDGET", 1), patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            with self.assertRaises(KeyboardInterrupt):
                format_dialogue(segments, "合成录音", config, self.folder, lambda *a: None)
            self.hook = None; self.requests.clear()
            format_dialogue(segments, "合成录音", config, self.folder, lambda *a: None)
        self.assertEqual([(phase, p["owned_parts"]) for phase, p, _ in self.requests], [("meeting_punctuation", [1])])
        self.assertEqual(read_json(self.folder / "formatting-run.json")["checkpointHits"], 1)
        self.assertEqual(read_json(self.folder / "dialogue-results.json")["notes"], notes)

    def test_failed_product_is_retained_for_diagnosis_without_reasoning(self):
        def malformed(client, body, phase, payload):
            if phase == "meeting_draft": return self.envelope({"overview": {"text": "test", "u": [999]}})
        self.hook = malformed
        with self.assertRaises(ValueError): self.run_reader()
        report = read_json(self.folder / "meeting-validation.json")
        self.assertEqual(report["failureCount"], 2)
        self.assertEqual(report["failures"][0]["phase"], "meeting_draft")
        self.assertEqual(set(report["failures"][0]), {"phase", "error", "candidate"})
        self.assertFalse((self.folder / "dialogue-results.json").exists())

    def test_retry_receives_source_bounds_and_multiple_attribution_locations(self):
        drafts = []
        def invalid_first(client, body, phase, payload):
            if phase != "meeting_draft": return None
            drafts.append(body)
            if len(drafts) == 1:
                value = document(self.engine.units)
                value["chapters"][0]["claims"] = [item(self.engine.units[0], speaker="A") for _ in range(8)]
                value["chapters"][0]["claims"][0]["evidence"][0]["last_part"] = 999
                return self.envelope(value)
        self.hook = invalid_first
        _, _ = self.run_reader(dialogueStrategy="meeting-baseline")
        self.assertEqual(len(drafts), 2)
        feedback = drafts[1]["messages"][-1]["content"]
        self.assertIn("chapters[0].claims[0]", feedback)
        self.assertIn("chapters[0].claims[7]", feedback)  # Beyond the old 240-character cut.
        self.assertIn("实际范围为0..3", feedback)
        self.assertIn("0:B", feedback)
        report = read_json(self.folder / "meeting-validation.json")
        failed = report["failures"][0]["candidate"]["chapters"][0]["claims"]
        self.assertEqual([i["speaker"] for i in failed], ["A"] * 8)
        self.assertEqual(failed[0]["evidence"][0]["last_part"], 999)  # Never cropped silently.
        self.assertEqual(report["failureCount"], 1)

    def test_retry_retains_original_candidate_without_expanded_quotes_or_reasoning(self):
        original = None
        def invalid_first(client, body, phase, payload):
            nonlocal original
            if phase != "meeting_draft": return None
            if len(self.requests) == 1:
                original = compact_evidence(document(self.engine.units))
                original["chapters"][0]["claims"][0].update(speaker="A", conditions=["仅此项目"], scope="合成范围")
                result = self.envelope(original)
                result["choices"][0]["message"]["reasoning_content"] = "PRIVATE_REASONING_NOT_FOR_REPLAY"
                return result
            messages = body["messages"]
            self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
            self.assertEqual(json.loads(messages[2]["content"]), original)
            self.assertNotIn("PRIVATE_REASONING_NOT_FOR_REPLAY", json.dumps(messages))
            self.assertNotIn("quote", messages[2]["content"])
            self.assertEqual(messages[0], self.requests[0][2]["messages"][0])
            fixed = copy.deepcopy(original)
            fixed["chapters"][0]["claims"][0]["speaker"] = "B"
            return self.envelope(fixed)
        self.hook = invalid_first
        engine, (notes, _) = self.run_reader(dialogueStrategy="meeting-baseline")
        metadata = notes["chapters"][0]["claims"][0]["metadata"]
        self.assertEqual(metadata["conditions"], ["仅此项目"])
        self.assertEqual(metadata["scope"], "合成范围")
        self.assertEqual(len(engine.client.validation_retries), 1)
        self.assertTrue(engine.client.validation_retries[0]["withinBudget"])
        saved = read_json(engine.client.directory / engine.client.validation_retries[0]["candidatePath"])
        self.assertEqual(json.loads(saved["candidate"]), original)
        self.assertNotIn("PRIVATE_REASONING_NOT_FOR_REPLAY", json.dumps(saved))
        before = len(self.requests)
        self.run_reader(dialogueStrategy="meeting-baseline")
        self.assertEqual(len(self.requests), before)

    def test_candidate_repair_capacity_stops_before_second_request_without_truncation(self):
        for config in ({"summaryInputBudget": 4000}, {"summaryContextWindow": 5000}):
            with self.subTest(config=config):
                e = self.reader(dialogueOutputTokens=256, dialogueMaxOutputTokens=256, **config)
                self.requests.clear()
                original = {"text": "原" * 4000}
                self.hook = lambda *a: self.envelope(original)
                def reject(value):
                    raise ValueError("合成的来源错误")
                with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
                    with self.assertRaisesRegex(UserError, "带原候选的修正请求超过"):
                        e.call("合成测试", {"sources": "完整原文"}, reject, "meeting_draft")
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(e.client.validation_retries[0]["withinBudget"])
                self.assertEqual(read_json(self.folder / "meeting-validation.json")["failures"][0]["candidate"], original)
                saved = read_json(e.client.directory / e.client.validation_retries[0]["candidatePath"])
                self.assertEqual(json.loads(saved["candidate"]), original)

    def test_shared_client_keeps_historical_retry_messages(self):
        client = ChatClient(self.config, self.folder / "legacy-client", lambda *a: None)
        requests = []
        def respond(body, phase):
            requests.append(copy.deepcopy(body))
            return self.envelope({"valid": len(requests) == 2})
        def validate(value):
            if not value["valid"]: raise ValueError("invalid")
        with patch.object(client, "_request", respond):
            result = client.complete("old prompt", {"source": "unchanged"}, validate)
        self.assertTrue(result["valid"])
        self.assertEqual(len(requests[1]["messages"]), 2)
        self.assertTrue(requests[1]["messages"][0]["content"].startswith("old prompt 上次结构或来源检查未通过"))
        self.assertEqual(requests[1]["messages"][1], requests[0]["messages"][1])

    def test_identity_isolates_strategy_effort_source_and_limits(self):
        original = self.reader().identity
        for opts in ({"dialogueStrategy": "meeting-baseline"}, {"reasoningEffort": "low"}, {"dialogueMaxRereads": 0}, {"sourceRevision": "changed"}):
            self.assertNotEqual(self.reader(**opts).identity, original)
        self.assertEqual(self.reader(beforePublish=lambda: None).identity, original)

    def test_expanded_validation_feedback_is_reserved_before_request_fits(self):
        e = self.reader()
        payload = {"sources": "一段完整原稿。"}
        old_size = Reader.request_size(e, "test", payload)
        e.input_budget = old_size + 1
        self.assertFalse(e.fits("test", payload))
        # With sufficient headroom it fits; no source text was shortened.
        e.input_budget = e.request_size("test", payload)
        self.assertTrue(e.fits("test", payload))
        self.assertEqual(payload["sources"], "一段完整原稿。")

    def test_quotes_reject_rewritten_terms_and_unknown_speaker_promotion(self):
        e = self.reader(); claim = item(e.units[0])
        bad = copy.deepcopy(claim); bad["evidence"][0]["quote"] = "中文材料准备好了。"
        with self.assertRaises(ValueError): validate_item(bad, e.lookup)
        bad = copy.deepcopy(claim); bad["speaker"] = "A"
        with self.assertRaises(ValueError): validate_item(bad, e.lookup)
        bad = item(e.units[0], "不", speaker="A", attribution_parts=[2])
        with self.assertRaisesRegex(ValueError, r"speaker=A.*2:unknown"):
            validate_item(bad, e.lookup)
        self.assertEqual(bad["speaker"], "A")  # Rejected, never silently reassigned.
        self.assertEqual(bad["attribution_parts"], [2])

    def test_evidence_parts_take_exact_source_without_model_retyping_or_guessing(self):
        e = self.reader()
        proof = anchor({"unit": 0, "first_part": 0, "last_part": 2}, e.lookup)
        self.assertEqual(proof["quote"], "中文资料已经有了。实验条件也齐备。先复现程序，测试通过才发送结果。不")
        self.assertEqual(proof["parts"], [0, 1, 2])
        self.assertEqual(anchor(proof, e.lookup), proof)
        with self.assertRaises(ValueError): anchor({**proof, "quote": proof["quote"][:-1]}, e.lookup)
        with self.assertRaises(ValueError): anchor({"unit": 0, "first_part": 0, "last_part": 999}, e.lookup)
        repeated = build_context([{**transcript()[0], "text": "是", "speaker": "A"}, {**transcript()[1], "text": "是", "speaker": "B"}])
        located = anchor({"unit": 0, "first_part": 1, "last_part": 1}, {0: repeated[0]})
        self.assertEqual((located["start"], located["end"], located["parts"]), (1, 2, [1]))

    def test_compact_review_evidence_round_trips_without_losing_claim_or_source(self):
        e = self.reader(); original = document(e.units)
        validate_document(original, e.units)
        compact = compact_evidence(original)
        self.assertNotIn("quote", compact["chapters"][0]["claims"][0]["evidence"][0])
        self.assertNotIn("unit", compact["chapters"][0]["claims"][0]["evidence"][0])
        self.assertIn("quote", original["chapters"][0]["claims"][0]["evidence"][0])
        validate_document(compact, e.units)
        self.assertEqual(compact, original)
        legacy = {"evidence": [{"unit": 0, "quote": "旧引用"}]}
        self.assertEqual(compact_evidence(legacy), legacy)

    def test_global_part_ranges_cross_transport_units_without_losing_text_or_attribution(self):
        segments = [{"id": i, "start": i * 30., "end": i * 30. + 2., "text": text, "speaker": speaker}
                    for i, (text, speaker) in enumerate([("暂不执行。", "B"), ("否", "unknown"), ("下周再讨论。", "A")])]
        e = self.reader(segments)
        proofs = evidence_anchors([{"first_part": 0, "last_part": 2}], e.lookup)
        self.assertEqual([p["unit"] for p in proofs], [0, 1, 2])
        self.assertEqual("".join(p["quote"] for p in proofs), "".join(s["text"] for s in segments))
        self.assertEqual(evidence_anchors(proofs, e.lookup), proofs)
        claim = {"text": "先不执行，之后再讨论。", "evidence": [{"first_part": 0, "last_part": 2}]}
        validate_item(claim, e.lookup)
        self.assertEqual(claim["u"], [0, 1, 2])
        unknown = {**claim, "speaker": "B", "attribution_parts": [1]}
        with self.assertRaisesRegex(ValueError, "1:unknown"):
            validate_item(unknown, e.lookup)
        self.assertEqual(unknown["speaker"], "B")  # Not silently reassigned.
        inventory = validate_evidence_item({"text": claim["text"], "evidence": [{"first_part": 0, "last_part": 2}]}, e.lookup)
        self.assertEqual(inventory["evidence"], proofs)

    def test_global_ranges_reject_missing_parts_and_do_not_repair_conflicting_legacy_coordinates(self):
        units = build_context([{**s, "start": i * 30., "end": i * 30. + 2.} for i, s in enumerate(transcript())])
        all_units = {u["id"]: u for u in units}
        subset = {0: units[0], 2: units[2]}
        for proof in ({"first_part": 0, "last_part": 999999999}, {"first_part": True, "last_part": 0},
                      {"first_part": 2, "last_part": 0}, {"first_part": 0, "last_part": 2}):
            with self.subTest(proof=proof), self.assertRaises(ValueError): evidence_anchors([proof], subset)
        with self.assertRaises(ValueError): evidence_anchors([{"first_part": 0, "last_part": 0, "quote": "篡改"}], all_units)
        with self.assertRaises(ValueError): evidence_anchors([{"unit": 0, "first_part": 0, "last_part": 1}], all_units)
        with self.assertRaises(ValueError): evidence_anchors([{"unit": 1, "first_part": 0, "last_part": 0}], all_units)

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
        first_batch = [{**split[0], "parts": [split[0]["parts"][0]]}]
        with self.assertRaises(ValueError):
            punctuation_rows({"rows": [{"first": 0, "last": 0}], "insertions": [{"part_id": 0, "offset": 1, "text": "，"}]}, first_batch, all_units=split)

    def test_punctuation_text_anchor_is_exact_unique_and_persists_character_offset(self):
        units = build_context([{**transcript()[0], "text": "嗯🙂 Large-v3 很好，保留  空格", "speaker": "unknown"}])
        original = units[0]["parts"][0]["text"]
        base = {"rows": [{"first": 0, "last": 0}], "insertions": []}
        valid = {"part_id": 0, "after": "Large-v3", "text": "，"}
        rows = punctuation_rows({**base, "insertions": [valid]}, units)
        part = rows[0]["parts"][0]
        self.assertEqual(part["text"], "嗯🙂 Large-v3， 很好，保留  空格")
        self.assertEqual(part["punctuation_insertions"], [{"offset": 11, "text": "，"}])
        self.assertEqual(part["speaker"], "unknown")
        self.assertEqual(units[0]["parts"][0]["text"], original)
        for invalid in ({**valid, "after": "large-v3"}, {**valid, "after": "保留 空格"},
                        {**valid, "after": "Large"}, {**valid, "after": ""}, {**valid, "offset": 11}):
            with self.assertRaises(ValueError):
                punctuation_rows({**base, "insertions": [invalid]}, units)
        duplicate = build_context([{**transcript()[0], "text": "哈哈哈"}])
        with self.assertRaisesRegex(ValueError, "唯一"):
            punctuation_rows({**base, "insertions": [{"part_id": 0, "after": "哈哈", "text": "。"}]}, duplicate)

    def test_punctuation_anchor_does_not_cross_parts_and_reports_all_bad_positions(self):
        units = build_context([{**transcript()[0], "text": "2", "speaker": "A"},
                               {**transcript()[1], "text": ".05好", "speaker": "unknown"}])
        first = [{**units[0], "parts": units[0]["parts"][:1]}]
        base = {"rows": [{"first": 0, "last": 0}], "insertions": []}
        for phrase in ("2", "2.05", ".05"):
            with self.assertRaises(ValueError):
                punctuation_rows({**base, "insertions": [{"part_id": 0, "after": phrase, "text": "。"}]}, first, all_units=units)
        with self.assertRaises(ValueError) as error:
            punctuation_rows({**base, "insertions": [{"part_id": 0, "offset": 2, "text": "。"},
                                                     {"part_id": 0, "offset": 3, "text": "，"}]}, first)
        self.assertIn("insertions[0]", str(error.exception)); self.assertIn("insertions[1]", str(error.exception))
        self.assertIn("0..1", str(error.exception))

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

    def test_uncertainty_repair_feedback_names_its_actual_schema(self):
        e = self.prepared()
        e.document["uncertainties"] = [{"id": "uncertainty-0", "text": "是否已有资料？", "u": [0], "status": "unresolved"}]
        targets = e.meeting_targets()
        valid = {**empty_review(), "status": "unresolved", "reason": "保留待确认范围"}
        valid["patches"] = [{"target_id": "uncertainty-0", "action": "replace", "check_ids": [], "reason": "限定问题范围",
                             "replacement": {"text": "资料是否完整？", "u": [0], "status": "unresolved"}}]
        def malformed_first(client, body, phase, payload):
            value = copy.deepcopy(valid)
            if len(self.requests) == 1:
                value["patches"][0]["replacement"] = item(e.units[0])
            return self.envelope(value)
        self.hook = malformed_first
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            value = e.call(REREAD_PROMPT, {"sources": "合成测试"},
                lambda v: e.validate_repair(v, e.units, targets, {"uncertainty-0"}, {}, reread=True), "meeting_reread")
        self.assertEqual(len(self.requests), 2)
        feedback = self.requests[1][2]["messages"][-1]["content"]
        self.assertIn("uncertainty-0的target_kind=uncertainty", feedback)
        self.assertIn('"status":"unresolved或needs_audio"', feedback)
        self.assertEqual(value["patches"][0]["replacement"], valid["patches"][0]["replacement"])
        self.assertEqual(e.document["uncertainties"][0]["text"], "是否已有资料？")

    def test_audio_uncertainty_cannot_be_removed_scheduled_for_text_or_resolved(self):
        e = self.prepared()
        e.document["uncertainties"] = [{"id": "uncertainty-0", "text": "听不清专名。", "u": [0], "status": "needs_audio"}]
        targets = e.meeting_targets()
        remove = empty_review()
        remove["patches"] = [{"target_id": "uncertainty-0", "action": "remove", "replacement": None, "reason": "猜测已解决", "check_ids": []}]
        with self.assertRaisesRegex(ValueError, "需回听"):
            e.validate_repair(remove, e.units, targets, {"uncertainty-0"}, {})
        schedule = empty_review()
        schedule["issues"] = [{"target_id": "uncertainty-0", "field": "text", "kind": "audio", "question": "专名是什么？", "u": [0], "query": "", "status": "unresolved"}]
        with self.assertRaisesRegex(ValueError, "需回听"):
            e.validate_repair(schedule, e.units, targets, {"uncertainty-0"}, {})
        resolved = {**empty_review(), "status": "resolved", "reason": "仅文字猜测", "evidence": [{"unit": 0, "first_part": 0, "last_part": 0}]}
        with self.assertRaisesRegex(ValueError, "需回听"):
            e.validate_repair(resolved, e.units, targets, {"uncertainty-0"}, {}, reread=True)

    def test_text_resolution_with_evidence_hides_one_question_and_keeps_audit_source(self):
        e = self.prepared()
        question = {"id": "uncertainty-0", "text": "资料是否已经放入目录？", "u": [0], "status": "unresolved"}
        e.document["uncertainties"] = [copy.deepcopy(question)]
        proposal = empty_review()
        proposal["issues"] = [{"target_id": "uncertainty-0", "field": "text", "kind": "state", "question": question["text"], "u": [0], "query": "资料", "status": "unresolved"}]
        e.apply_repair(proposal, "review")
        self.hook = lambda *_: self.envelope({**empty_review(), "status": "resolved", "reason": "原话明确资料已经有了", "evidence": [{"first_part": 0, "last_part": 0}]})
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            e.reread()
        self.assertEqual(e.reread_count, 1)
        self.assertEqual(e.outputs()[0]["uncertainties"], [])
        self.assertEqual(e.document["uncertainties"], [question])
        proof = e.patch_log[-1]["proposal"]["evidence"][0]
        self.assertEqual({k: proof[k] for k in ("unit", "first_part", "last_part")}, {"unit": 0, "first_part": 0, "last_part": 0})
        self.assertEqual(anchor(proof, e.lookup)["quote"], e.units[0]["parts"][0]["text"])

    def test_registered_doubts_replace_the_draft_row_without_merging_distinct_fields(self):
        e = self.prepared()
        original = {"id": "uncertainty-0", "text": "总数和所指范围需要确认。", "u": [0], "status": "unresolved"}
        untouched = {"id": "uncertainty-1", "text": "另一个独立问题。", "u": [0], "status": "needs_audio"}
        e.document["uncertainties"] = [copy.deepcopy(original), copy.deepcopy(untouched)]
        proposal = empty_review()
        proposal["issues"] = [{"target_id": "uncertainty-0", "field": field, "kind": kind,
                               "question": "这里的说法需要回听。", "u": [0], "query": "", "status": "needs_audio"}
                              for field, kind in (("number", "number"), ("text", "audio"))]
        e.apply_repair(proposal, "review")
        notes, _ = e.outputs()
        questions = notes["uncertainties"]
        self.assertEqual(len(questions), 3)  # Two distinct fields, one unrelated doubt.
        registered = [q for q in questions if q.get("originalQuestion")]
        self.assertEqual(len(registered), 2)
        self.assertEqual(len({q["id"] for q in registered}), 2)
        self.assertTrue(all(q["originalQuestion"] == original["text"] for q in registered))
        self.assertEqual(e.document["uncertainties"], [original, untouched])
        for issue in e.issues.values():
            issue["status"] = "resolved"
        notes, _ = e.outputs()
        self.assertEqual([q["id"] for q in notes["uncertainties"]], ["uncertainty-1"])

    def test_reread_cannot_edit_unrelated_claim_in_the_same_source_range(self):
        e = self.prepared()
        unrelated = {**copy.deepcopy(e.document["chapters"][0]["claims"][0]), "id": "claim-other"}
        e.document["chapters"][0]["claims"].append(unrelated)
        p = empty_review()
        p["issues"] = [{"target_id": "claim-0", "field": "state", "kind": "state", "question": "资料有无准备？", "u": [0], "query": "资料", "status": "unresolved"}]
        e.apply_repair(p, "review")
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            e.reread()
        sent = next(payload for phase, payload, _ in self.requests if phase == "meeting_reread")
        self.assertEqual([target["id"] for target in sent["targets"]], ["claim-0"])
        self.assertEqual(e.document["chapters"][0]["claims"][1], unrelated)

    def test_input_limit_splits_complete_responsibility_ranges_and_preserves_end(self):
        # Each source is separated by a genuine gap; no arbitrary character cuts.
        segments = [{"id": i, "start": i * 10., "end": i * 10. + 2,
                     "text": f"第{i}项资料已具备。", "speaker": "B"} for i in range(8)]
        e = self.reader(segments)
        def constrained(prompt, payload, validator, phase):
            ids = [int(i) for i in re.findall(r"\[u(\d+)\]", payload.get("sources", ""))]
            if phase in ("meeting_draft", "meeting_evidence") and len(ids) > 2:
                raise InputContextLimit("synthetic context limit")
            if phase == "meeting_review" and len(payload["owned_checks"]) > 2:
                raise InputContextLimit("synthetic review limit")
            units = [u for u in e.units if u["id"] in ids]
            if phase == "meeting_merge":
                docs = payload["partial_documents"]
                self.assertTrue(all("quote" not in proof for d in docs for c in d["chapters"]
                                    for claim in c["claims"] for proof in claim["evidence"]))
                value = {"overview": docs[0]["overview"], "chapters": [c for d in docs for c in d["chapters"]],
                         "action_items": [], "uncertainties": []}
            elif phase == "meeting_draft":
                value = document(units)
                value["chapters"][0]["claims"] = [item(u, text=u["text"]) for u in units]
            elif phase == "meeting_evidence":
                value = {"items": [item(u, text=u["text"]) for u in units],
                         "coverage": [{"unit": i, "disposition": "substantive"} for i in payload["owned_units"]]}
            elif phase == "meeting_review":
                value = empty_review(payload["owned_checks"], payload["owned_targets"])
            else:
                raise AssertionError(phase)
            validator(value)
            return value
        with patch.object(e, "call", side_effect=constrained):
            e.identify(e.draft(e.units)); e.inventory(e.units); e.review_range(e.units)
        self.assertEqual(e.route, "hierarchical")
        self.assertEqual([c["u"][0] for ch in e.document["chapters"] for c in ch["claims"]], list(range(8)))
        self.assertEqual([u for c in e.source_coverage for u in c["ownedUnits"]], list(range(8)))
        self.assertEqual(e.reviewed, set(range(8)))
        self.assertEqual(len(e.assessments), 8)
        self.assertEqual(e.review_batches, 4)

    def test_compact_merge_rejects_source_evidence_not_present_in_parent_records(self):
        # Repeated wording is not permission to cite another person's occurrence.
        segments = [{"id": 0, "start": 0., "end": 2., "text": "资料已具备。", "speaker": "B"},
                    {"id": 1, "start": 2., "end": 4., "text": "资料已具备。", "speaker": "A"},
                    {"id": 2, "start": 30., "end": 33.,
                                   "text": "另一项工作尚未开始。", "speaker": "A"}]
        e = self.reader(segments)
        def constrained(prompt, payload, validator, phase):
            ids = [int(i) for i in re.findall(r"\[u(\d+)\]", payload.get("sources", ""))]
            if phase == "meeting_draft" and len(ids) > 1:
                raise InputContextLimit("synthetic limit")
            if phase == "meeting_merge":
                value = copy.deepcopy(payload["partial_documents"][0])
                # This source span exists, but the parent record did not cite it.
                value["chapters"][0]["claims"][0] = item(e.units[0], speaker="none", attribution_parts=[],
                    evidence=[{"unit": 0, "first_part": 1, "last_part": 1}])
            else:
                value = document([u for u in e.units if u["id"] in ids])
            validator(value)
            return value
        with patch.object(e, "call", side_effect=constrained):
            with self.assertRaisesRegex(ValueError, "分层合并只能使用"):
                e.draft(e.units)

    def test_local_patch_preserves_prior_result_and_records_state_condition_evidence(self):
        e = self.prepared(); before = copy.deepcopy(e.document)
        replacement = item(e.units[0], text="先复现程序", kind="proposal", state="pending", conditions=["测试通过才发送结果"])
        proposal = empty_review(e.checks, e.meeting_targets())
        proposal["patches"] = [{"target_id": "claim-0", "action": "replace", "replacement": replacement,
                               "reason": "保留动作的测试前提", "check_ids": list(e.checks)}]
        e.validate_repair(proposal, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)
        self.assertTrue(e.apply_repair(proposal, "review"))
        self.assertEqual(e.patch_log[0]["before"], before)
        self.assertEqual(e.document["chapters"][0]["claims"][0]["id"], "claim-0")
        self.assertEqual(e.document["chapters"][0]["claims"][0]["conditions"], ["测试通过才发送结果"])
        self.assertEqual(e.patch_log[0]["after"], e.document)

    def test_source_observations_do_not_promote_classifications_to_facts(self):
        e = self.reader()
        observation = item(e.units[0], text="中文资料已经有了。", kind="decision", state="pending", owner="B")
        result = validate_evidence_item(observation, e.lookup)
        self.assertEqual(set(result), {"text", "evidence", "u"})
        self.assertEqual(result["text"], observation["text"])
        self.assertEqual(result["evidence"][0]["quote"], transcript()[0]["text"])
        bad = {**observation, "evidence": [{"unit": 0, "quote": "不存在的原文"}]}
        with self.assertRaises(ValueError): validate_evidence_item(bad, e.lookup)

    def test_field_patch_preserves_unmentioned_conditions_scope_and_action_owner(self):
        e = self.prepared()
        action = item(e.units[0], text="发送结果。", kind="action", state="pending", owner="A",
                      conditions=["测试通过"], scope="本次试验", due="测试通过后",
                      evidence=[{"unit": 0, "first_part": 1, "last_part": 1}], speaker="A", attribution_parts=[1])
        validate_item(action, e.lookup, action=True)
        action["id"] = "action-0"
        e.document["action_items"].append(action)
        before = copy.deepcopy(action)
        repair = empty_review()
        repair["patches"] = [{"target_id": "action-0", "action": "update", "changes": {"text": "先复现程序，再发送结果。"},
                              "reason": "补回动作依赖，测试前提仍保留。", "check_ids": []}]
        for _ in range(2):
            e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)
        self.assertEqual(action, before)
        e.apply_repair(repair, "review")
        after = e.document["action_items"][0]
        for field in set(before) - {"text"}:
            self.assertEqual(after[field], before[field], field)
        rendered = e.cited(after)["text"]
        self.assertIn("测试通过", rendered)
        self.assertIn("本次试验", rendered)
        self.assertEqual(e.patch_log[0]["proposal"]["patches"][0]["changes"], {"text": "先复现程序，再发送结果。"})

    def test_field_patch_rejects_identity_unknown_fields_and_invalid_merged_attribution(self):
        e = self.prepared()
        for changes in ({}, {"id": "claim-fake"}, {"u": [999]}, {"future_option": True},
                        {"speaker": "A"}, {"evidence": [{"unit": 999, "first_part": 0, "last_part": 0}]}):
            with self.subTest(changes=changes):
                repair = empty_review()
                repair["patches"] = [{"target_id": "claim-0", "action": "update", "changes": changes,
                                      "reason": "合成非法字段或不一致来源", "check_ids": []}]
                before = copy.deepcopy(e.document)
                with self.assertRaises(ValueError):
                    e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)
                self.assertEqual(e.document, before)

    def test_field_patch_round_trip_resume_keeps_saved_sources_and_no_extra_requests(self):
        def repair(client, body, phase, payload):
            if phase == "meeting_review":
                return self.envelope({**empty_review(), "patches": [{"target_id": "claim-0", "action": "update",
                    "changes": {"text": "中文资料已经具备，实验条件也齐备。"},
                    "reason": "补回同一原文中资源条件；保留原状态和归属。", "check_ids": []}]})
        self.hook = repair
        _, (notes, _) = self.run_reader()
        claim = notes["chapters"][0]["claims"][0]
        self.assertEqual(claim["metadata"]["state"], "current")
        self.assertEqual(claim["source_ids"], [0])
        self.assertIn("实验条件", claim["text"])
        before = len(self.requests)
        _, (again, _) = self.run_reader()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(again["chapters"], notes["chapters"])
        self.assertEqual(again["tokensThisRun"], 0)

    def test_explicit_clear_and_uncertainty_field_patch_use_existing_guards(self):
        e = self.prepared()
        e.document["chapters"][0]["claims"][0]["scope"] = "错误的范围"
        repair = empty_review()
        repair["patches"] = [{"target_id": "claim-0", "action": "update", "changes": {"scope": None},
                              "reason": "原文没有该范围，不应保留。", "check_ids": []}]
        e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)
        e.apply_repair(repair, "review")
        self.assertIsNone(e.document["chapters"][0]["claims"][0]["scope"])
        e.document["uncertainties"] = [{"id": "uncertainty-0", "text": "需回听", "u": [0], "status": "needs_audio"}]
        repair["patches"] = [{"target_id": "uncertainty-0", "action": "update", "changes": {"text": "该词需回听确认。"},
                              "reason": "明确需要回听的内容。", "check_ids": []}]
        e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)
        e.apply_repair(repair, "review")
        self.assertEqual(e.document["uncertainties"][0]["status"], "needs_audio")
        repair["patches"][0]["changes"] = {"status": "unresolved"}
        with self.assertRaisesRegex(ValueError, "需回听"):
            e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)

    def test_review_may_add_exact_source_omission_absent_from_independent_inventory(self):
        e = self.prepared()
        repair = empty_review()
        repair["additions"] = [{"chapter_id": "topic-0", "item": item(e.units[0], text="实验条件也齐备。"),
                                "check_ids": [], "reason": "直接核对原文发现清单也遗漏了实验条件。"}]
        e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks, owned_units={0})
        e.apply_repair(repair, "review")
        self.assertEqual(len(e.document["chapters"][0]["claims"]), 2)
        with self.assertRaisesRegex(ValueError, "本批负责原文"):
            e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks, owned_units={99})
        repair["additions"][0]["check_ids"] = ["check-does-not-exist"]
        with self.assertRaises(ValueError):
            e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), e.checks)

    def test_statement_playback_uses_cited_parts_not_entire_reading_unit(self):
        e = self.prepared()
        claim = e.document["chapters"][0]["claims"][0]
        cited = e.cited(claim)
        self.assertEqual(cited["source_ids"], [0])
        self.assertEqual(cited["metadata"]["attribution_parts"], [0])
        # The overview still references the complete context it summarizes.
        self.assertEqual(e.cited(e.document["overview"], statement=False)["source_ids"], [0, 1, 2, 3])

    def test_withdrawn_proposal_patch_keeps_history_and_both_source_positions(self):
        segments = [
            {"id": 0, "start": 0., "end": 2., "text": "建议明天交付。", "speaker": "A"},
            {"id": 1, "start": 2., "end": 4., "text": "我不同意，测试尚未通过。", "speaker": "B"},
            {"id": 2, "start": 4., "end": 6., "text": "那收回明天交付的提议，时间另议。", "speaker": "A"}]
        e = self.reader(segments)
        d = document(e.units)
        d["chapters"][0]["claims"][0] = item(e.units[0], text="建议明天交付。", kind="proposal", state="pending")
        validate_document(d, e.units); e.identify(d)
        before = copy.deepcopy(e.document)
        replacement = item(e.units[0], text="曾建议明天交付，随后收回；时间另议。", kind="proposal", state="withdrawn",
                           attribution_parts=[0, 2], evidence=[{"unit": 0, "first_part": 0, "last_part": 0},
                                                               {"unit": 0, "first_part": 2, "last_part": 2}])
        repair = empty_review()
        repair["patches"] = [{"target_id": "claim-0", "action": "replace", "replacement": replacement,
                               "reason": "发起人在后文明确撤回，保留提议与变化过程。", "check_ids": []}]
        e.validate_repair(repair, e.units, e.meeting_targets(), set(e.meeting_targets()), {})
        e.apply_repair(repair, "review")
        claim = e.document["chapters"][0]["claims"][0]
        self.assertEqual(e.patch_log[0]["before"], before)
        self.assertEqual(claim["id"], "claim-0")
        self.assertEqual(e.cited(claim)["source_ids"], [0, 2])
        self.assertIn("已撤回", e.cited(claim)["text"])
        self.assertNotIn("待完成", e.cited(claim)["text"])
        self.assertEqual(e.document["action_items"], [])

    def test_cross_range_correction_does_not_reassign_claim_to_another_review_batch(self):
        segments = [{"id": i, "start": i * 10., "end": i * 10. + 2,
                     "text": f"第{i}项资料已具备。", "speaker": "B"} for i in range(4)]
        e = self.reader(segments)
        d = document(e.units)
        d["chapters"][0]["claims"][0]["evidence"].append({"unit": 3, "quote": e.units[3]["text"]})
        validate_document(d, e.units); e.identify(d)
        for u in e.units:
            c = item(u, text=u["text"]); validate_item(c, e.lookup); c["id"] = f"check-{u['id']}"; e.checks[c["id"]] = c
        assigned = []
        def split(prompt, payload, validator, phase):
            if len(payload["owned_checks"]) > 2: raise InputContextLimit("synthetic review limit")
            assigned.extend(payload["owned_targets"])
            v = empty_review(payload["owned_checks"], [t["id"] for t in payload["targets"]])
            if "claim-0" in payload["owned_targets"]:
                v["patches"] = [{"target_id": "claim-0", "action": "replace", "replacement": item(e.units[3], text=e.units[3]["text"]),
                                 "reason": "后文更正以前文为准的陈述", "check_ids": []}]
            validator(v); return v
        with patch.object(e, "call", side_effect=split): e.review_range(e.units)
        self.assertEqual(assigned.count("claim-0"), 1)
        self.assertEqual(e.document["chapters"][0]["claims"][0]["u"], [3])
        self.assertEqual(e.review_target_owners["claim-0"], 0)
        self.assertEqual(e.reviewed, set(range(4)))

    def test_cancel_after_independent_evidence_resumes_successful_stages(self):
        def stop(client, body, phase, payload):
            if phase == "meeting_review": raise KeyboardInterrupt()
        self.hook = stop
        with self.assertRaises(KeyboardInterrupt): self.run_reader()
        self.assertFalse((self.folder / "dialogue-results.json").exists())
        previous = len(self.requests); self.hook = None
        _, _ = self.run_reader()
        self.assertEqual([p for p, _, _ in self.requests[previous:]], ["meeting_review"])

    def test_long_review_keeps_distant_source_without_importing_unrelated_claims(self):
        segments = [{"id": i, "start": i * 10., "end": i * 10. + 2,
                     "text": f"第{i}项资料已经具备。", "speaker": "B"} for i in range(12)]
        e = self.reader(segments)
        d = document(e.units)
        d["overview"]["u"] = [0, 3, 5, 8, 10]
        d["chapters"][0]["claims"] = [item(u, text=u["text"]) for u in e.units]
        validate_document(d, e.units); e.identify(d)
        for u in e.units:
            c = item(u, text=u["text"]); validate_item(c, e.lookup)
            c["id"] = f"check-{u['id']}"; e.checks[c["id"]] = c
        batches = []
        def bounded(prompt, payload, validator, phase):
            if len(payload["targets"]) > 4 or len(payload["owned_checks"]) > 2:
                raise InputContextLimit("synthetic complete-payload limit")
            batches.append(copy.deepcopy(payload))
            v = empty_review(payload["owned_checks"], payload["owned_targets"])
            validator(v); return v
        with patch.object(e, "call", side_effect=bounded): e.review_range(e.units)
        overview_batch = next(p for p in batches if "overview" in p["owned_targets"])
        self.assertIn("[u10]", overview_batch["sources"])
        self.assertIn("第10项资料已经具备。", overview_batch["sources"])
        self.assertNotIn("claim-10", [t["id"] for t in overview_batch["targets"]])
        self.assertCountEqual([t for p in batches for t in p["owned_targets"]], e.meeting_targets())
        self.assertCountEqual([c for p in batches for c in p["owned_checks"]], e.checks)
        self.assertEqual(e.reviewed, set(range(12)))
        self.assertEqual(e.document["overview"]["u"], [0, 3, 5, 8, 10])

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

    def route_reader(self, **options):
        config = {**self.config, "baseURL": "https://api.icompify.com/v1", "model": "deepseek-v4.1-flash"}
        config.pop("dialogueMaxOutputTokens")
        self.engine = MeetingReader(transcript(), "预算测试", {**config, **options}, self.folder, lambda *a: None)
        return self.engine

    def test_current_route_uses_128k_then_256k_without_changing_effort_or_frozen_reader(self):
        for effort in ("default", "low", "high", "max"):
            e = self.route_reader(reasoningEffort=effort)
            self.assertEqual((e.initial_output, e.max_output), (131072, 262144))
            self.assertEqual(e.client.reasoning_effort, effort)
        frozen = Reader(transcript(), "预算测试", {"baseURL": "https://api.icompify.com/v1", "model": "deepseek-v4.1-flash",
                        "apiKey": "test-only", "reasoningEffort": "high"}, self.folder, lambda *a: None)
        self.assertEqual((frozen.initial_output, frozen.max_output), (32768, 131072))
        other = self.reader(baseURL="https://api.icompify.com/v1", model="other-model", dialogueMaxOutputTokens=32768)
        self.assertEqual((other.initial_output, other.max_output), (32768, 32768))
        self.assertNotEqual(e.identity, self.route_reader(dialogueMaxOutputTokens=131072).identity)
        for config in ({"dialogueMaxOutputTokens": 262145}, {"dialogueOutputTokens": 262144, "dialogueMaxOutputTokens": 131072}):
            with self.assertRaises(UserError): self.route_reader(**config)

    def test_new_output_budgets_are_sent_and_successful_replay_has_no_new_requests(self):
        self.hook = lambda client, body, phase, payload: (self.envelope({"ok": True}) if body["max_tokens"] == 262144 else
            {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "not retained"}}],
             "usage": {"completion_tokens": 131072}})
        for iteration in range(2):
            e = self.route_reader()
            with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
                value = e.call("预算测试", {}, lambda v: self.assertTrue(v["ok"]), "meeting_draft")
            self.assertTrue(value["ok"])
        self.assertEqual([body["max_tokens"] for _, _, body in self.requests], [131072, 262144])
        self.assertEqual(e.client.cache_hits, 1)
        self.assertEqual(e.client.limit_hits, 1)

    def test_256k_exhaustion_or_capacity_rejection_stops_without_third_attempt(self):
        for final_error in (ReasoningOutputExhausted, ResponseOutputLimit, InputContextLimit):
            e = self.route_reader(); calls = []
            def complete(prompt, payload, validator, output_tokens, phase):
                calls.append(output_tokens)
                if len(calls) == 1: raise ReasoningOutputExhausted()
                raise final_error()
            expected = UserError if final_error == InputContextLimit else final_error
            with patch.object(e.client, "complete", complete), self.assertRaises(expected): e.draft(e.units)
            self.assertEqual(calls, [131072, 262144])
            self.assertEqual(e.route, "whole")
        e = self.route_reader()
        with patch.object(e.client, "complete", side_effect=UserError("接口不接受请求参数（HTTP 400）。")) as request:
            with self.assertRaisesRegex(UserError, "262,144"):
                e.draft(e.units)
        self.assertEqual(request.call_count, 1)

    def test_confirmed_context_limit_still_bounds_expansion_and_changes_identity(self):
        e = self.route_reader(summaryContextWindow=200000)
        seen = []
        def complete(prompt, payload, validator, output_tokens, phase):
            seen.append(output_tokens)
            if len(seen) == 1: raise ReasoningOutputExhausted()
            return {"ok": True}
        with patch.object(e.client, "complete", complete): e.call("test", {}, lambda v: None, "meeting_draft")
        self.assertEqual(seen[0], 131072)
        self.assertGreater(seen[1], seen[0])
        self.assertLess(seen[1], 200000)
        self.assertNotEqual(e.identity, self.route_reader().identity)

    def test_worker_accepts_new_strategies_and_rejects_unknown(self):
        for strategy in ("meeting-v3", "meeting-baseline", "context-v2", "legacy"):
            validate_request({"sourceKind": "recording", "title": "测试", "dialogueStrategy": strategy})
        with self.assertRaises(UserError): validate_request({"sourceKind": "recording", "title": "测试", "dialogueStrategy": "typo"})
        validate_request({"sourceKind": "recording", "title": "测试", "dialogueStrategy": "meeting-v3", "recordingAction": "format"})
        with self.assertRaises(UserError):
            validate_request({"sourceKind": "recording", "title": "测试", "dialogueStrategy": "legacy", "recordingAction": "format"})


if __name__ == "__main__":
    unittest.main()
