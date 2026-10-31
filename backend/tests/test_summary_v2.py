import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import UserError, read_json
from summary import ChatClient, OutputLimit, ReasoningOutputExhausted, summarize
from summary_v2 import (Reader, paragraphs, source_text, partition, validate_whole, validate_review,
                        apply_review, read_request, claim_index, coalesce_reads)


def transcript(count=20):
    return [{"id": i, "start": i * 5., "end": i * 5. + 4.,
             "text": f"第{i}个论点。学习率为0.01，仅在固定条件下成立，结尾修正不得遗漏。" * 2} for i in range(count)]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.config = {"baseURL": "https://example.com/v1", "model": "fake", "apiKey": "test-only",
                       "summaryStrategy": "structured", "concurrency": 1}
        self.network = []
    def tearDown(self):
        self.tmp.cleanup()

    def response(self, client, body, phase="summary"):
        self.network.append(phase)
        payload = json.loads(body["messages"][1]["content"])
        if phase == "plan":
            a, b = payload["first"], payload["last"]
            mid = (a + b) // 2
            value = {"theme": "条件与更正", "chapters": [
                {"first": a, "last": mid, "title": "前提", "focus": "条件"},
                {"first": mid + 1, "last": b, "title": "结论", "focus": "限制"}],
                "terms": [], "connections": [], "uncertainties": []}
        elif phase in ("chapter", "whole"):
            a, b = payload["owned_first"], payload["owned_last"]
            value = {"claims": [{"p": [a], "text": "学习率0.01，仅在固定条件下成立。"}],
                     "condensed": [{"first": a, "last": b, "text": "全部片段的顺序精简。"}]}
            if phase == "whole":
                value.update(overview="条件概览", chapters=[{"title": "学习率", "claims": value.pop("claims")}],
                             key_points=[{"p": [a], "text": "有条件的学习率"}], uncertainties=[])
        elif phase in ("verify", "reread"):
            value = {"fixes": [], "additions": [], "uncertainties": [], "read_requests": []}
            if "summary_draft" in payload:
                value["summary"] = payload["summary_draft"]
        elif phase == "synthesis":
            value = {"overview": "学习率与条件", "key_points": [{"claim_ids": [payload["claims"][0]["id"]],
                     "text": "学习率0.01，仅在固定条件下成立。"}], "uncertainties": [], "resolved_findings": []}
        elif phase == "final_check":
            value = payload["draft"]
        else:
            raise AssertionError(phase)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}

    def test_transport_is_lossless_and_mapping_handles_huge_caption(self):
        values = transcript(30)
        values[5]["text"] = "🧪attention中文" * 1000
        rows = paragraphs(values)
        self.assertEqual("".join(p["text"] for p in rows), "".join(s["text"] for s in values))
        self.assertEqual({i for p in rows for i in p["source_ids"]}, set(range(30)))
        self.assertTrue(all(len(p["text"]) <= 480 for p in rows))
        self.assertLess(len(source_text(paragraphs(transcript())).encode()), len(json.dumps(transcript()).encode()))

    def test_plan_or_condensed_cannot_skip_reorder_overlap_or_invent_ranges(self):
        rows = paragraphs(transcript())
        last = len(rows) - 1
        partition([{"first": 0, "last": last}], rows)
        for ranges in ([{"first": 1, "last": last}], [{"first": 0, "last": last + 1}],
                       [{"first": 0, "last": 0}, {"first": 0, "last": last}],
                       [{"first": 0, "last": last - 1}], [{"first": True, "last": last}]):
            with self.assertRaises(ValueError): partition(ranges, rows)

    def test_reread_is_literal_and_only_accesses_transcript(self):
        rows = paragraphs(transcript(40))
        self.assertEqual(read_request({"p": [], "query": "../../.env", "reason": "test"}, rows), [])
        self.assertEqual(read_request({"p": [], "query": ".*", "reason": "test"}, rows), [])
        selected = read_request({"p": [3], "query": "", "reason": "上下文"}, rows)
        self.assertEqual([p["id"] for p in selected], [2, 3, 4])
        self.assertEqual(read_request({"p": [-1, 9999, True], "query": "", "reason": "test"}, rows), [])

    def test_review_cannot_cite_unread_source_or_edit_other_claim(self):
        chapters = [{"title": "test", "claims": [{"p": [0], "text": "旧说法"}, {"p": [1], "text": "更正"}]}]
        lookup = claim_index(chapters)
        review = {"fixes": [{"claim_id": "c0.0", "action": "replace", "p": [0, 1], "text": "保留更正"}],
                  "additions": [], "uncertainties": [], "read_requests": []}
        with self.assertRaises(ValueError): validate_review(review, {0}, lookup, {0}, {0, 1})
        validate_review(review, {0, 1}, lookup, {0}, {0, 1})
        review["fixes"][0]["claim_id"] = "c99.0"
        with self.assertRaises(ValueError): validate_review(review, {0, 1}, lookup, {0}, {0, 1})

    def test_removing_a_claim_does_not_shift_subsequent_revision_target(self):
        chapters = [{"title": "test", "claims": [{"p": [i], "text": str(i)} for i in range(3)]}]
        lookup = claim_index(chapters)
        value = {"fixes": [{"claim_id": "c0.0", "action": "remove", "p": [0], "text": "无依据"},
                            {"claim_id": "c0.2", "action": "replace", "p": [2], "text": "已更正"}], "additions": []}
        apply_review(chapters, value, lookup)
        self.assertEqual([c["text"] for c in chapters[0]["claims"]], ["1", "已更正"])

    def test_short_reads_whole_and_uses_validated_checkpoints_on_resume(self):
        with patch.object(ChatClient, "_request", autospec=True, side_effect=self.response):
            result, condensed = summarize(transcript(), "短视频", self.config, self.folder, lambda *a: None)
            before = list(self.network)
            again, _ = summarize(transcript(), "短视频", self.config, self.folder, lambda *a: None)
        self.assertEqual(result["summaryRoute"], "whole")
        self.assertNotIn("plan", self.network)
        self.assertEqual(before, self.network)
        self.assertEqual(again["tokensThisRun"], 0)
        self.assertEqual(result["reviewedParagraphs"], result["paragraphCount"])
        self.assertEqual([s for item in condensed for s in item["source_ids"]], list(range(20)))
        self.assertNotIn("test-only", "".join(p.read_text() for p in self.folder.rglob("*.json")))

    def test_interrupted_long_read_reuses_plan_and_completed_chapters(self):
        values = transcript(150)
        fail = True
        def response(client, body, phase="summary"):
            if fail and phase == "chapter" and self.network.count("chapter") == 1:
                raise UserError("受控中断")
            return self.response(client, body, phase)
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            with self.assertRaises(UserError): summarize(values, "长视频", self.config, self.folder, lambda *a: None)
            cached = {p.name: p.read_bytes() for p in (self.folder / "checkpoints/summary-v2").glob("*.json")}
            fail = False
            result, condensed = summarize(values, "长视频", self.config, self.folder, lambda *a: None)
        self.assertEqual(self.network.count("plan"), 1)
        self.assertEqual(result["summaryRoute"], "planned")
        self.assertEqual([s for item in condensed for s in item["source_ids"]], list(range(150)))
        self.assertTrue(all((self.folder / "checkpoints/summary-v2" / n).read_bytes() == b for n, b in cached.items()))
        self.assertGreater(read_json(self.folder / "summary-run.json")["checkpointHits"], 0)

    def test_budget_stops_before_request_and_truncation_never_becomes_checkpoint(self):
        client = ChatClient({**self.config, "summaryMaxRequests": 0}, self.folder, lambda *a: None)
        with self.assertRaises(UserError): client._request({}, "plan")
        self.assertFalse(list(self.folder.glob("*.json")))

    def test_truncated_response_records_usage_but_cannot_become_a_checkpoint(self):
        client = ChatClient(self.config, self.folder, lambda *a: None)
        result = {"choices": [{"finish_reason": "length", "message": {"content": '{"partial":'}}],
                  "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}}
        with patch.object(ChatClient, "_request", return_value=result):
            with self.assertRaises(OutputLimit): client.complete("review", {}, lambda v: None)
        self.assertEqual(client.total_tokens, 300)
        self.assertFalse(list(self.folder.glob("*.json")))
        self.assertTrue((self.folder / "usage.jsonl").exists())

    def test_short_review_corrects_overview_and_notes_without_regenerating_them(self):
        def response(client, body, phase="summary"):
            value = self.response(client, body, phase)
            if phase == "verify":
                payload = json.loads(body["messages"][1]["content"])
                key = next(c for c in payload["draft"] if c.get("kind") == "key_point")
                overview = next(c for c in payload["draft"] if c.get("kind") == "overview")
                result = json.loads(value["choices"][0]["message"]["content"])
                result["fixes"] = [{"claim_id": c["id"], "action": "replace", "p": [0], "text": "作者称：仅在固定条件下成立。"}
                                   for c in (key, overview)]
                value["choices"][0]["message"]["content"] = json.dumps(result)
            return value
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            result, _ = summarize(transcript(), "短视频", self.config, self.folder, lambda *a: None)
        self.assertEqual(self.network, ["whole", "verify"])
        self.assertEqual(result["overview"], "作者称：仅在固定条件下成立。")
        self.assertEqual(result["key_points"][0]["text"], result["overview"])
        self.assertEqual(len(result["chapters"]), 1)

    def test_reread_is_bounded_and_resumes_after_a_review_interruption(self):
        rows = paragraphs(transcript(180))
        chapters = [{"title": "前提", "claims": [{"p": [0], "text": "条件"}]}]
        plan = {"chapters": [{"first": 0, "last": len(rows) - 1}], "connections": [
            {"kind": "correction", "p": [i * 2, i * 2 + 17], "text": f"核对第{i}处条件"} for i in range(8)]}
        fail = True
        def response(client, body, phase="summary"):
            if fail and phase == "reread" and self.network.count("reread") == 1:
                raise UserError("受控核验中断")
            return self.response(client, body, phase)
        def run():
            reader = Reader(rows, "长视频", self.config, self.folder, lambda *a: None)
            reader.plan = plan
            return reader.review(copy.deepcopy(chapters))
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            with self.assertRaises(UserError): run()
            prior = self.network.count("verify")
            fail = False
            uncertainties = run()
        report = read_json(self.folder / "review-report.json")
        self.assertEqual(self.network.count("verify"), prior)
        self.assertEqual(self.network.count("reread"), 6)
        self.assertEqual(report["rereadCount"], 6)
        self.assertEqual(report["reviewedParagraphs"], list(range(len(rows))))
        self.assertEqual(len(uncertainties), 2)

    def test_review_reads_complete_cross_chapter_citations(self):
        rows = paragraphs(transcript(150))
        last = len(rows) - 1
        reader = Reader(rows, "前文被结尾修正", self.config, self.folder, lambda *a: None)
        reader.plan = {"chapters": [{"first": 0, "last": last}], "connections": []}
        chapters = [{"title": "有条件的结论", "claims": [{"p": [0, last], "text": "原先说法已撤回。"}]}]
        def response(client, body, phase="summary"):
            payload = json.loads(body["messages"][1]["content"])
            for claim in payload["draft"]:
                for p in claim["p"]:
                    self.assertIn(f"[p{p}] ", payload["sources"])
            return self.response(client, body, phase)
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            reader.review(chapters)
        requests = [{"p": [0, last], "query": "", "reason": "年代更正"},
                    {"p": [1, last], "query": "", "reason": "后文撤回"}]
        merged = coalesce_reads(requests, rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["p"], [0, 1, last])
        self.assertIn("年代更正", merged[0]["reason"])
        self.assertIn("后文撤回", merged[0]["reason"])

    def test_resume_skips_a_known_truncated_budget_before_using_larger_checkpoint(self):
        reader = Reader(paragraphs(transcript()), "预算恢复", self.config, self.folder, lambda *a: None)
        calls = []
        def response(client, body, phase="summary"):
            calls.append(body["max_tokens"])
            if body["max_tokens"] == 16384:
                return {"choices": [{"finish_reason": "length", "message": {"content": '{"valid":'}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 16384, "total_tokens": 16484}}
            return {"choices": [{"finish_reason": "stop", "message": {"content": '{"valid":true}'}}]}
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            reader.review_call({}, lambda v: self.assertTrue(v["valid"]), "verify")
            # Simulate a termination after metering was durable but before the
            # limit marker was written (also covers older development records).
            for marker in (reader.client.directory / "limits").glob("*.json"):
                marker.unlink()
            again = Reader(reader.rows, reader.title, self.config, self.folder, lambda *a: None)
            again.review_call({}, lambda v: self.assertTrue(v["valid"]), "verify")
        self.assertEqual(calls, [16384, 24576])
        self.assertEqual(again.client.limit_hits, 1)
        self.assertEqual(again.client.cache_hits, 1)

    def test_high_review_expands_to_128k_and_resumes_without_rebilling(self):
        for successful_limit in (32768, 131072, None):
            with self.subTest(successful_limit=successful_limit):
                config = {**self.config, "reasoningEffort": "high"}
                folder = self.folder / str(successful_limit)
                reader = Reader(paragraphs(transcript()), "高强度核验", config, folder, lambda *a: None)
                calls = []
                def response(client, body, phase="summary"):
                    limit = body["max_tokens"]
                    calls.append(limit)
                    if limit == successful_limit:
                        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"valid":true}'}}]}
                    return {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "private"}}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": limit, "total_tokens": 100 + limit,
                                      "completion_tokens_details": {"reasoning_tokens": limit}}}
                def review(target):
                    if successful_limit:
                        self.assertEqual(target.review_call({}, lambda v: self.assertTrue(v["valid"]), "verify"), {"valid": True})
                    else:
                        with self.assertRaises(ReasoningOutputExhausted):
                            target.review_call({}, lambda v: None, "verify")
                with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
                    review(reader)
                expected_limits = [16384, 32768] if successful_limit == 32768 else [16384, 32768, 131072]
                self.assertEqual(calls, expected_limits)
                again = Reader(reader.rows, reader.title, config, folder, lambda *a: None)
                with patch.object(ChatClient, "_request", side_effect=AssertionError("No repeated paid request")):
                    review(again)
                self.assertEqual(again.client.cache_hits, 1 if successful_limit else 0)
                self.assertEqual(again.client.limit_hits, len(expected_limits) - (1 if successful_limit else 0))
                self.assertNotIn("private", (reader.client.directory / "usage.jsonl").read_text())

    def test_legacy_resume_reuses_split_children_without_repeating_truncated_parent(self):
        config = {**self.config, "summaryStrategy": "legacy"}
        directory = self.folder / "checkpoints" / "summary"
        calls = []
        def response(client, body, phase="summary"):
            payload = json.loads(body["messages"][1]["content"])
            calls.append(payload)
            if payload == [0, 1]:
                return {"choices": [{"finish_reason": "length", "message": {"content": '{"valid":'}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292}}
            return {"choices": [{"finish_reason": "stop", "message": {"content": '{"valid":true}'}}]}
        def resume(client):
            try:
                return client.complete("legacy reduction", [0, 1], lambda v: self.assertTrue(v["valid"]))
            except OutputLimit:
                return [client.complete("legacy reduction", [i], lambda v: self.assertTrue(v["valid"])) for i in (0, 1)]
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            expected = resume(ChatClient(config, directory, lambda *a: None))
        again = ChatClient(config, directory, lambda *a: None)
        with patch.object(ChatClient, "_request", side_effect=AssertionError("Unexpected new HTTP request")):
            self.assertEqual(resume(again), expected)
        self.assertEqual(calls, [[0, 1], [0], [1]])
        self.assertEqual((again.limit_hits, again.cache_hits), (1, 2))

    def test_unresolved_review_findings_cannot_disappear_from_a_short_overview(self):
        def response(client, body, phase="summary"):
            value = self.response(client, body, phase)
            payload = json.loads(body["messages"][1]["content"])
            if phase == "verify" and 0 in payload["owned"]:
                result = json.loads(value["choices"][0]["message"]["content"])
                result["uncertainties"] = [{"p": [0], "text": "年份在原文中有两种解释，尚未消歧。"}]
                value["choices"][0]["message"]["content"] = json.dumps(result)
            return value
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            result, _ = summarize(transcript(150), "数字歧义", self.config, self.folder, lambda *a: None)
        self.assertIn("年份在原文中有两种解释，尚未消歧。", [x["text"] for x in result["uncertainties"]])
        ledger = read_json(self.folder / "finding-resolutions.json")
        self.assertEqual(len(ledger["findings"]), 1)
        self.assertEqual(ledger["resolved"], [])

    def test_overview_cannot_close_source_ambiguity_remaining_after_reread(self):
        reader = Reader(paragraphs(transcript()), "日期歧义", self.config, self.folder, lambda *a: None)
        reader.protected_findings = {0}
        chapters = [{"title": "日期", "claims": [{"p": [0], "text": "说法含混。"}]}]
        def response(client, body, phase="summary"):
            value = self.response(client, body, phase)
            result = json.loads(value["choices"][0]["message"]["content"])
            result["resolved_findings"] = [{"finding_id": "f0", "claim_ids": ["c0.0"], "reason": "改写后已经确定"}]
            value["choices"][0]["message"]["content"] = json.dumps(result)
            return value
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            reader.synthesis(chapters, [{"p": [0], "text": "回读后日期仍有两种解释。"}])
        self.assertEqual(reader.resolved_findings, set())
        self.assertEqual(read_json(self.folder / "finding-resolutions.json")["protectedAfterReread"], ["f0"])


if __name__ == "__main__":
    unittest.main()
