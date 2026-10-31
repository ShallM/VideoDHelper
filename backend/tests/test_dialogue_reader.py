import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import UserError, read_json, write_json
from dialogue_context import build_context
from dialogue_reader import (Reader, SUMMARY_PROMPT, condensed_batches, summarize,
                             validate_condensed, validate_summary, _part_text_limit,
                             validate_sparse_condensed, project_condensed, validate_reader_text)
from summary import ChatClient, InputContextLimit, ResponseOutputLimit, ReasoningOutputExhausted


def transcript(count=3):
    phrases = ["只有测试通过，才可以提交；此前先保留结果。", "嗯", "费用是2.05元，不是20.5元；明天再确认。"]
    return [{"id": i, "start": i * 2., "end": i * 2. + 1.5,
             "text": phrases[i % 3], "speaker": ("A", "unknown", "B")[i % 3]} for i in range(count)]


def review():
    return {"patches": [], "additions": [], "issues": [], "resolutions": []}


def summary_value(units):
    ids = [u["id"] for u in units][:12]
    return {"overview": {"text": "提交有前提，费用待确认。", "u": ids},
            "chapters": [{"title": "测试和费用", "claims": [{"text": "测试通过后才能提交。", "u": ids},
                                                          {"text": "费用仍需确认。", "u": ids}]}],
            "key_points": [], "uncertainties": []}


def condensed_value(units):
    return {"rows": [{"parts": [{"first": p["id"], "last": p["id"], "speaker": p["speaker"], "text": p["text"]}
                                for u in units for p in u["parts"]]}]}


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.config = {"baseURL": "https://example.com/v1", "model": "fake", "apiKey": "test-only",
                       "reasoningEffort": "high", "dialogueMaxOutputTokens": 131072}
        self.network = []
        self.hook = None

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, segments=None, **config):
        return Reader(segments or transcript(), "合成测试", {**self.config, **config}, self.folder, lambda *args: None)

    def response(self, client, body, phase="summary"):
        client.request_count += 1
        payload = json.loads(body["messages"][1]["content"])
        self.network.append((phase, payload, body["max_tokens"]))
        if self.hook:
            result = self.hook(client, body, phase, payload)
            if result is not None:
                return result
        if phase in ("dialogue_summary", "dialogue_merge"):
            ids = [int(v) for v in re.findall(r"\[u(\d+)\]", payload["sources"])] or payload["allowed_u"]
            value = summary_value([{"id": v} for v in ids])
        elif phase == "dialogue_condense":
            value = {"rows": [{"first": payload["owned_parts"][0], "last": payload["owned_parts"][-1]}], "edits": []}
        elif phase in ("dialogue_review", "dialogue_reread"):
            value = review()
        else:
            raise AssertionError(phase)
        return self.envelope(value)

    @staticmethod
    def envelope(value):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}

    def run_reader(self, segments=None, **config):
        engine = self.reader(segments, **config)
        with patch.object(ChatClient, "_request", lambda client, body, phase: self.response(client, body, phase)):
            result = engine.run()
        return engine, result

    def prepared(self, segments=None):
        engine = self.reader(segments)
        engine.assign_ids(summary_value(engine.units), condensed_value(engine.units)["rows"])
        return engine

    def test_whole_input_separate_products_and_resume_without_new_requests(self):
        engine, (notes, rows) = self.run_reader(transcript(150))
        phases = [p for p, _, _ in self.network]
        self.assertEqual(phases.count("dialogue_summary"), 1)
        self.assertEqual(phases.count("dialogue_review"), 1)
        self.assertNotIn("dialogue_merge", phases)
        self.assertEqual("".join(r["text"] for r in rows), "".join(s["text"] for s in transcript(150)))
        self.assertEqual({p["speaker"] for r in rows for p in r["parts"]}, {"A", "B", "unknown"})
        self.assertTrue(all(p["spans"] for r in rows for p in r["parts"]))
        self.assertEqual(notes["reviewedParagraphs"], notes["paragraphCount"])
        self.assertEqual(notes["summaryRoute"], "whole")
        first_calls = len(self.network)
        resumed, _ = self.run_reader(transcript(150))
        self.assertEqual(len(self.network), first_calls)
        self.assertEqual(resumed.client.cache_hits, len(phases))

    def test_identity_includes_word_timing_diarization_effort_not_publish_callback(self):
        segments = transcript()
        a = self.reader(segments)
        changed = copy.deepcopy(segments)
        changed[0]["words"] = [{"word": changed[0]["text"], "start": 0.1, "end": 1.4}]
        self.assertNotEqual(a.identity, self.reader(changed).identity)
        self.assertNotEqual(a.identity, self.reader(segments, diarizationIdentity="changed").identity)
        self.assertNotEqual(a.identity, self.reader(segments, reasoningEffort="low").identity)
        self.assertEqual(a.identity, self.reader(segments, beforePublish=lambda: None).identity)
        self.assertEqual(a.identity, self.reader(segments, dialogueMaxRereads=0).identity)

    def test_json_object_mode_is_opt_in_except_verified_provider_model(self):
        regular = self.reader()
        self.assertFalse(regular.config["jsonObjectMode"])
        verified = self.reader(baseURL="https://api.icompify.com/v1", model="deepseek-v4.1-flash")
        self.assertTrue(verified.config["jsonObjectMode"])
        opted_out = self.reader(baseURL="https://api.icompify.com/v1", model="deepseek-v4.1-flash", jsonObjectMode=False)
        self.assertFalse(opted_out.config["jsonObjectMode"])
        self.assertNotEqual(verified.identity, opted_out.identity)
        self.assertFalse(self.reader(baseURL="https://api.icompify.com/v1", model="other-model").config["jsonObjectMode"])
        enabled = self.reader(jsonObjectMode=True)
        self.assertGreater(enabled.request_size(SUMMARY_PROMPT, {}), regular.request_size(SUMMARY_PROMPT, {}))
        with self.assertRaises(UserError): self.reader(jsonObjectMode="true")

    def test_schema_disallows_unknown_guess_coverage_loss_and_output_bloat(self):
        units = build_context(transcript())
        good = condensed_value(units)
        validate_condensed(good, units)
        for change in ("speaker", "omission", "duplicate", "short", "expansion", "huge_range"):
            bad = copy.deepcopy(good)
            parts = bad["rows"][0]["parts"]
            if change == "speaker": parts[1]["speaker"] = "A"
            elif change == "omission": parts.pop(1)
            elif change == "duplicate": parts.append(parts[0])
            elif change == "short": parts[1]["text"] = "嗯，同意"
            elif change == "huge_range": parts[0]["last"] = 10 ** 30
            else: parts[0]["text"] += "补充解释" * 20
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_condensed(bad, units)

    def test_sparse_rows_derive_mixed_speakers_and_keep_unedited_characters(self):
        segments = transcript()
        units = build_context(segments)
        value = {"rows": [{"first": 0, "last": 2}], "edits": [{"part_id": 1, "text": "嗯。"}]}
        validate_sparse_condensed(value, units)
        expanded = project_condensed(value, units)
        parts = expanded["rows"][0]["parts"]
        self.assertEqual([p["speaker"] for p in parts], ["A", "unknown", "B"])
        self.assertEqual(parts[0]["text"], segments[0]["text"])
        self.assertEqual(parts[2]["text"], segments[2]["text"])
        self.assertIn("只有测试通过，才可以提交", parts[0]["text"])
        engine = self.reader(segments)
        engine.assign_ids(summary_value(units), expanded["rows"])
        row = engine.outputs()[1][0]
        self.assertNotIn("speaker", row)
        self.assertEqual([p["speaker"] for p in row["parts"]], ["A", "unknown", "B"])
        self.assertTrue(all(p["spans"] for p in row["parts"]))

    def test_sparse_edits_reject_invalid_id_duplicates_and_lost_short_text(self):
        units = build_context(transcript())
        base = {"rows": [{"first": 0, "last": 2}], "edits": []}
        bad_edits = [
            [{"part_id": 9, "text": "其他片段"}],
            [{"part_id": True, "text": "嗯"}],
            [{"part_id": 1, "text": "嗯"}, {"part_id": 1, "text": "嗯。"}],
            [{"part_id": 1, "text": ""}], [{"part_id": 1, "text": "  "}],
            [{"part_id": 1, "text": "同意"}],
            [{"part_id": 1, "text": "嗯", "speaker": "A"}],
            [{"part_id": 0, "text": "额外解释" * 30}],
        ]
        for edits in bad_edits:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                validate_sparse_condensed({**base, "edits": edits}, units)
        short = build_context([{"id": 0, "start": 0., "end": 1., "text": "了他", "speaker": "unknown"}])
        with self.assertRaises(ValueError):
            validate_sparse_condensed({"rows": [{"first": 0, "last": 0}], "edits": [{"part_id": 0, "text": "了"}]}, short)

    def test_sparse_boundaries_cannot_skip_repeat_reorder_or_invent_ownership(self):
        units = build_context(transcript())
        invalid = [[], [{"first": 1, "last": 2}], [{"first": 0, "last": 1}],
                   [{"first": 0, "last": 1}, {"first": 1, "last": 2}],
                   [{"first": 1, "last": 2}, {"first": 0, "last": 0}],
                   [{"first": 0, "last": 10 ** 30}], [{"first": False, "last": 2}],
                   [{"first": 0, "last": 2, "speaker": "A"}]]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_sparse_condensed({"rows": rows, "edits": []}, units)

    def test_stable_targets_do_not_shift_and_patch_cannot_change_ownership(self):
        engine = self.prepared()
        value = review()
        value["patches"] = [
            {"target_kind": "claim", "target_id": "claim-0", "action": "remove", "text": "重复", "u": [0]},
            {"target_kind": "claim", "target_id": "claim-1", "action": "replace", "text": "待确认2.05元。", "u": [0]}]
        engine.validate_review(value, {0}, engine.targets())
        engine.apply_review(value)
        claims = engine.document["chapters"][0]["claims"]
        self.assertEqual([(v["id"], v["text"]) for v in claims], [("claim-1", "待确认2.05元。")])
        for extra in ({"speaker": "A"}, {"target_kind": "claim"}, {"target_id": "claim-404"}):
            bad = review()
            bad["patches"] = [{"target_kind": "dialogue_part", "target_id": "dialogue-part-1", "action": "replace", "text": "嗯", "u": [0], **extra}]
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                engine.validate_review(bad, {0}, engine.targets(include_unchanged=True))

    def test_patch_aggregate_growth_is_bounded(self):
        engine = self.prepared(transcript(30))
        value = review()
        candidates = [p for row in engine.dialogue for p in row["parts"] if len(p["text"]) > 2][:12]
        value["patches"] = [{"target_kind": "dialogue_part", "target_id": p["id"], "action": "replace",
                             "text": p["text"] + "补充五个字呀！", "u": p["u"]} for p in candidates]
        with self.assertRaisesRegex(ValueError, "整体"):
            engine.validate_review(value, set(engine.lookup), engine.targets(include_unchanged=True))

    def test_punctuation_is_not_expansion_but_short_source_cannot_lose_characters(self):
        raw = "先完成检查再确认金额如果结果有变化明天继续讨论" * 6
        punctuated = "先完成检查，再确认金额；如果结果有变化，明天继续讨论。" * 6
        segments = [{"id": 0, "start": 0., "end": 30., "text": raw, "speaker": "A"}]
        units = build_context(segments)
        value = condensed_value(units)
        value["rows"][0]["parts"][0]["text"] = punctuated
        validate_condensed(value, units)
        self.assertEqual(units[0]["text"], raw)
        self.assertEqual(value["rows"][0]["parts"][0]["text"], punctuated)
        engine = self.prepared(segments)
        update = review()
        update["patches"] = [{"target_kind": "dialogue_part", "target_id": "dialogue-part-0",
                              "action": "replace", "text": punctuated, "u": [0]}]
        engine.validate_review(update, {0}, engine.targets(include_unchanged=True))
        update["patches"][0]["text"] += "这里可以解释出未说过的额外道理" * 4
        with self.assertRaises(ValueError): engine.validate_review(update, {0}, engine.targets(include_unchanged=True))
        for original, changed in (("了他", "了，他。"), ("不我", "不，我"), ("好。", "好。！")):
            _part_text_limit(changed, [{"text": original}])
        for original, changed in (("了他", "了"), ("好。", "好"), ("你", "您"), ("好吗", "吗好"), (" 嗯", "嗯")):
            with self.subTest(original=original, changed=changed), self.assertRaises(ValueError):
                _part_text_limit(changed, [{"text": original}])

    def test_review_skips_only_exact_copy_parts_but_retains_full_source_and_summary(self):
        engine = self.prepared()
        originals = copy.deepcopy(engine.parts)
        self.assertFalse(any(t["kind"] == "dialogue_part" for t in engine.targets().values()))
        engine.dialogue[0]["parts"][0]["text"] += "。"
        engine.dialogue[0]["parts"][1]["text"] += " "
        targets = engine.targets()
        self.assertIn("dialogue-part-0", targets)
        self.assertIn("dialogue-part-1", targets)
        self.assertNotIn("dialogue-part-2", targets)
        self.assertIn("overview", targets)
        self.assertIn("claim-0", targets)
        self.assertIn("claim-1", targets)
        payload, units, actual = engine.review_payload(engine.units, list(targets))
        self.assertEqual(units, engine.units)
        self.assertIn("[part2:B]" + originals[2]["text"], payload["sources"])
        self.assertEqual(set(actual), set(targets))
        self.assertEqual(engine.parts, originals)

    def test_review_owns_six_units_with_full_context_and_only_intersecting_targets(self):
        segments = transcript(13)
        for index, segment in enumerate(segments):
            segment.update(start=index * 10., end=index * 10. + 1.)
        engine = self.prepared(segments)
        self.assertEqual(len(engine.units), 13)
        engine.document["overview"]["u"] = [0]
        engine.document["chapters"][0]["claims"] = [{"id": f"claim-{i}", "text": "待核验的条件", "u": [i]} for i in range(13)]
        for row in engine.dialogue:
            for part in row["parts"]:
                part["text"] += "。"
        requests = []
        def complete(prompt, payload, validator, **kwargs):
            requests.append(copy.deepcopy(payload))
            value = review(); validator(value); return value
        with patch.object(engine.client, "complete", complete): engine.review_all()
        self.assertEqual([p["owned_sources"] for p in requests], [list(range(6)), list(range(6, 12)), [12]])
        self.assertEqual(engine.reviewed, set(range(13)))
        self.assertEqual(engine.full_context_review_batches, 3)
        for payload in requests:
            self.assertEqual(set(map(int, re.findall(r"\[u(\d+)\]", payload["sources"]))), set(range(13)))
            owned = set(payload["owned_sources"])
            self.assertIn("overview", {t["id"] for t in payload["targets"]})
            for target in payload["targets"]:
                if target["kind"] == "dialogue_part": self.assertTrue(set(target["u"]).issubset(owned))
                elif target["kind"] != "overview": self.assertTrue(set(target["u"]) & owned)
        short = self.prepared()
        with patch.object(short.client, "complete", complete): short.review_all()
        self.assertEqual(short.review_batches, 1)

    def test_full_context_review_rejection_falls_back_once_then_preserves_coverage(self):
        engine = self.prepared()
        modes = []
        def complete(prompt, payload, validator, **kwargs):
            modes.append(payload["source_context"])
            if payload["source_context"] == "full": raise InputContextLimit()
            value = review(); validator(value); return value
        with patch.object(engine.client, "complete", complete): engine.review_all()
        self.assertEqual(modes, ["full", "owned_and_evidence"])
        self.assertEqual(engine.reviewed, set(engine.lookup))
        self.assertEqual(engine.full_context_review_batches, 0)

    def test_internal_ids_are_rejected_in_reader_text_but_real_source_code_survives(self):
        for word in ("u4", "part12", "claim-7"):
            with self.subTest(word=word), self.assertRaises(ValueError):
                validate_reader_text(f"需要按{word}说明处理", ["这里没有代码变量。"])
            validate_reader_text(f"代码变量{word}的值为2", [f"定义{word}=2"])
        with self.assertRaises(ValueError): validate_reader_text("变量u4", ["变量u40"])
        validate_reader_text("input_u4与part12_count是代码名", ["程序片段"])
        units = build_context(transcript())
        for field in ("overview", "title", "claim", "key_points", "uncertainties"):
            value = summary_value(units)
            if field == "overview": value[field]["text"] = "来源u4不清楚"
            elif field == "title": value["chapters"][0]["title"] = "来源part12"
            elif field == "claim": value["chapters"][0]["claims"][0]["text"] = "claim-7的解释"
            else: value[field] = [{"text": "来源u4的条件", "u": [0]}]
            with self.subTest(field=field), self.assertRaises(ValueError): validate_summary(value, units)
        # A same-named variable in an unrelated unit is not evidence for this claim.
        separate = [{"id": 0, "text": "普通内容"}, {"id": 1, "text": "代码u4=2"}]
        value = summary_value(separate)
        value["chapters"][0]["claims"][0] = {"text": "代码u4", "u": [0]}
        with self.assertRaises(ValueError): validate_summary(value, separate)
        value["chapters"][0]["claims"][0]["u"] = [1]
        value["chapters"][0]["title"] = "代码u4"
        validate_summary(value, separate)

    def test_patch_addition_issue_body_cannot_leak_transport_ids(self):
        engine = self.prepared()
        for field in ("patches", "additions", "issues"):
            value = review()
            if field == "patches":
                value[field] = [{"target_kind": "claim", "target_id": "claim-0", "action": "replace", "text": "请按u4核对", "u": [0]}]
            elif field == "additions":
                value[field] = [{"chapter_id": "chapter-0", "text": "part12的结论", "u": [0]}]
            else:
                value[field] = [{"kind": "ambiguity", "question": "claim-7是什么意思？", "u": [0], "query": "", "status": "unresolved"}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                engine.validate_review(value, {0}, engine.targets())
        valid = review()
        valid["patches"] = [{"target_kind": "claim", "target_id": "claim-0", "action": "replace", "text": "保留明确前提。", "u": [0]}]
        engine.validate_review(valid, {0}, engine.targets())

    def test_output_exhaustion_expands_once_without_fragmenting_summary(self):
        for error in (ResponseOutputLimit, ReasoningOutputExhausted):
            engine = self.reader()
            calls = []
            def complete(prompt, payload, validator, output_tokens, phase):
                calls.append(output_tokens)
                raise error()
            with patch.object(engine.client, "complete", complete), self.assertRaises(error):
                engine.summary(engine.units)
            self.assertEqual(calls, [32768, 131072])
            self.assertEqual(engine.route, "whole")

    def test_combined_limit_after_expansion_does_not_start_recursive_calls(self):
        engine = self.reader(transcript(100))
        calls = []
        def complete(prompt, payload, validator, output_tokens, phase):
            calls.append(output_tokens)
            if len(calls) == 1: raise ReasoningOutputExhausted()
            raise InputContextLimit()
        with patch.object(engine.client, "complete", complete), self.assertRaisesRegex(UserError, "合计容量"):
            engine.summary(engine.units)
        self.assertEqual(calls, [32768, 131072])

    def test_actual_input_limit_alone_uses_bounded_hierarchy(self):
        seen = False
        def limit_once(client, body, phase, payload):
            nonlocal seen
            if not seen and phase == "dialogue_summary":
                seen = True
                raise InputContextLimit()
        self.hook = limit_once
        engine, (notes, _) = self.run_reader(transcript(70))
        self.assertEqual(notes["summaryRoute"], "hierarchical")
        self.assertEqual([p for p, _, _ in self.network].count("dialogue_merge"), 1)
        self.assertTrue(engine.reviewed)

    def test_output_batches_keep_unit_ids_and_neighbour_context(self):
        segments = [{"id": i, "start": i * .1, "end": (i + 1) * .1, "text": "好", "speaker": "A" if i % 2 else "unknown"} for i in range(260)]
        units = build_context(segments)
        self.assertEqual(len(units), 1)
        groups = condensed_batches(units)
        self.assertEqual([sum(len(u["parts"]) for u in g) for g in groups], [120, 120, 20])
        self.assertTrue(all(u["id"] == 0 for g in groups for u in g))
        engine, (_, rows) = self.run_reader(segments)
        batches = [payload for phase, payload, _ in self.network if phase == "dialogue_condense"]
        self.assertEqual(len(batches), 3)
        self.assertTrue(all("[part259:" in p["context"] for p in batches))
        self.assertEqual("".join(r["text"] for r in rows), "好" * 260)
        self.assertEqual(engine.condense_batches, 3)

    def test_read_limit_and_issue_dedup_preserve_unresolved_and_audio(self):
        engine = self.prepared(transcript(100))
        value = review()
        for index, kind in enumerate(("condition", "negation", "number", "attribution", "agreement", "correction")):
            value["issues"].append({"kind": kind, "question": f"第{index}个需要核对的条件", "u": [0], "query": "", "status": "unresolved"})
        engine.validate_review(value, set(engine.lookup), engine.targets())
        engine.apply_review(value)
        engine.apply_review(copy.deepcopy(value))
        self.assertEqual(len(engine.issues), 6)
        with patch.object(engine.client, "complete", lambda *args, **kwargs: review()) as complete:
            engine.reread()
        self.assertEqual(engine.reread_count, 6)
        self.assertEqual(len(engine.outputs()[0]["uncertainties"]), 6)
        stopped = self.prepared()
        stopped.max_rereads = 0
        stopped.issues = copy.deepcopy(engine.issues)
        with patch.object(stopped.client, "complete", side_effect=AssertionError("unexpected call")):
            stopped.reread()
        audio = review()
        audio["issues"] = [{"kind": "audio", "question": "需要听音确认", "u": [0], "query": "", "status": "unresolved"}]
        engine.validate_review(audio, set(engine.lookup), engine.targets())
        engine.apply_review(audio)
        issue = next(i for i in engine.issues.values() if i["kind"] == "audio")
        close = review(); close["resolutions"] = [{"issue_id": issue["id"], "u": [0], "reason": "猜测"}]
        with self.assertRaises(ValueError): engine.validate_review(close, {0}, engine.targets())

    def test_same_source_different_questions_remain_independently_resolvable(self):
        engine = self.prepared()
        value = review()
        value["issues"] = [{"kind": "number", "question": q, "u": [0], "query": "", "status": "unresolved"}
                           for q in ("金额是什么？", "日期是什么？")]
        engine.validate_review(value, {0}, engine.targets()); engine.apply_review(value)
        ids = list(engine.issues)
        self.assertEqual(len(ids), 2)
        close = review(); close["resolutions"] = [{"issue_id": ids[0], "u": [0], "reason": "金额有明确原话"}]
        engine.validate_review(close, {0}, engine.targets()); engine.apply_review(close)
        self.assertEqual(engine.issues[ids[1]]["status"], "unresolved")
        unmatched = review()
        unmatched["issues"] = [{"kind": "omission", "question": "另一个金额？", "u": [], "query": "无匹配文字", "status": "unresolved"}]
        engine.validate_review(unmatched, {0}, engine.targets()); engine.apply_review(unmatched)
        with patch.object(engine.client, "complete", lambda *args, **kwargs: review()): engine.reread()
        missing = next(i for i in engine.issues.values() if i["query"])
        self.assertEqual(missing["status"], "unresolved")
        self.assertEqual(missing["diagnostic"], "no_source_match")
        self.assertTrue(all(i["source_ids"] for i in engine.outputs()[0]["uncertainties"]))

    def test_publish_guard_preserves_previous_products_and_successful_checkpoints(self):
        write_json(self.folder / "notes.json", {"prior": True})
        write_json(self.folder / "condensed.json", [{"prior": True}])
        write_json(self.folder / "dialogue-results.json", {"priorGeneration": True})
        def changed(): raise UserError("原稿已修改")
        with self.assertRaisesRegex(UserError, "原稿已修改"):
            self.run_reader(beforePublish=changed)
        self.assertEqual(read_json(self.folder / "notes.json"), {"prior": True})
        self.assertEqual(read_json(self.folder / "condensed.json"), [{"prior": True}])
        self.assertEqual(read_json(self.folder / "dialogue-results.json"), {"priorGeneration": True})
        self.assertFalse(read_json(self.folder / "summary-run.json")["completed"])
        self.assertTrue(list((self.folder / "checkpoints").rglob("*.json")))

    def test_atomic_bundle_survives_failure_between_compatibility_mirrors(self):
        write_json(self.folder / "condensed.json", [{"previous": True}])
        def fail_mirror(path, value):
            if Path(path).name == "condensed.json":
                raise OSError("synthetic disk failure")
            write_json(path, value)
        with patch("dialogue_reader.write_json", fail_mirror), self.assertRaises(OSError):
            self.run_reader()
        bundle = read_json(self.folder / "dialogue-results.json")
        self.assertEqual(bundle["schemaVersion"], 1)
        self.assertEqual(bundle["notes"]["sourceRevision"], self.reader().source_revision)
        self.assertEqual("".join(r["text"] for r in bundle["condensed"]), "".join(s["text"] for s in transcript()))
        self.assertEqual(read_json(self.folder / "condensed.json"), [{"previous": True}])
        self.assertTrue(read_json(self.folder / "summary-run.json")["published"])
        self.assertFalse(read_json(self.folder / "summary-run.json")["completed"])

    def test_cancelled_later_stage_reuses_successful_summary(self):
        def cancel(client, body, phase, payload):
            if phase == "dialogue_condense": raise KeyboardInterrupt()
        self.hook = cancel
        with self.assertRaises(KeyboardInterrupt): self.run_reader()
        self.assertFalse((self.folder / "dialogue-results.json").exists())
        self.hook = None
        engine, _ = self.run_reader()
        self.assertEqual([p for p, _, _ in self.network].count("dialogue_summary"), 1)
        self.assertEqual(engine.client.cache_hits, 1)

    def test_schema_repair_has_one_retry_and_request_budget_counts_draft(self):
        def invalid(client, body, phase, payload):
            if phase == "dialogue_summary": return self.envelope({"overview": {"text": "missing"}})
        self.hook = invalid
        with self.assertRaises(ValueError): self.run_reader()
        self.assertEqual(len(self.network), 2)
        engine = self.reader()
        self.assertGreater(engine.request_size(SUMMARY_PROMPT, {"draft": "原文" * 10000}),
                           engine.request_size(SUMMARY_PROMPT, {}))
        for value in (-1, 7, True):
            with self.assertRaises(UserError): self.reader(dialogueMaxRereads=value)


if __name__ == "__main__":
    unittest.main()
