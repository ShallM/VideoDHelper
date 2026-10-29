import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import UserError, read_json, write_json
from dialogue_context import build_context
from meeting_contract import validate_item, validate_document, punctuation_rows, render_item, anchor
from meeting_reader import MeetingReader, compact_evidence
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
        self.assertIn("quote", original["chapters"][0]["claims"][0]["evidence"][0])
        validate_document(compact, e.units)
        self.assertEqual(compact, original)
        legacy = {"evidence": [{"unit": 0, "quote": "旧引用"}]}
        self.assertEqual(compact_evidence(legacy), legacy)

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
        self.assertEqual([c["unit"] for c in e.source_coverage], list(range(8)))
        self.assertEqual(e.reviewed, set(range(8)))
        self.assertEqual(len(e.assessments), 8)
        self.assertEqual(e.review_batches, 4)

    def test_compact_merge_rejects_source_evidence_not_present_in_parent_records(self):
        segments = transcript() + [{"id": 4, "start": 30., "end": 33.,
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
