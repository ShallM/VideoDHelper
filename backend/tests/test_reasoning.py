import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from common import UserError, write_json
from summary import ChatClient, OutputLimit, InputContextLimit, ResponseOutputLimit, ReasoningOutputExhausted, reasoning_effort


class ReasoningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.config = {"baseURL": "http://127.0.0.1:8123/v1", "model": "compatible-model", "apiKey": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, effort=None, **overrides):
        config = {**self.config, **overrides}
        if effort is not None:
            config["reasoningEffort"] = effort
        return ChatClient(config, self.folder, lambda *args: None)

    @staticmethod
    def response(content='{"answer":"yes"}', reason="stop", reasoning=None):
        message = {"content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return {"choices": [{"finish_reason": reason, "message": message}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}}

    @staticmethod
    def validate(value):
        if value != {"answer": "yes"}:
            raise ValueError("unexpected result")

    def test_selected_strength_is_sent_and_has_distinct_checkpoints(self):
        seen = []
        for effort in ("default", "low", "medium", "high", "max"):
            client = self.client(effort)
            def request(body, **kwargs):
                seen.append(dict(body))
                return self.response()
            with patch.object(client, "_request", side_effect=request) as network:
                self.assertEqual(client.complete("prompt", {"source": "text"}, self.validate), {"answer": "yes"})
                self.assertEqual(network.call_count, 1)
                client.complete("prompt", {"source": "text"}, self.validate)
                self.assertEqual(network.call_count, 1, "identical strength must reuse its checkpoint")
            self.assertEqual(seen[-1].get("reasoning_effort"), None if effort == "default" else effort)
            self.assertEqual(client.calls[0]["reasoningEffort"], effort)
        self.assertEqual(len(list(self.folder.glob("*.json"))), 5)
        entries = [json.loads(line) for line in (self.folder / "usage.jsonl").read_text().splitlines()]
        self.assertEqual([entry["reasoningEffort"] for entry in entries], ["default", "low", "medium", "high", "max"])

    def test_legacy_default_is_low_but_provider_default_omits_parameter(self):
        client = self.client()
        with patch.object(client, "_request", return_value=self.response()) as request:
            client.complete("prompt", {"source": "text"}, self.validate)
        self.assertEqual(request.call_args.args[0]["reasoning_effort"], "low")
        self.assertEqual(reasoning_effort({"model": "deepseek-v4.1-flash", "reasoningEffort": "default"}), "default")

    def test_explicit_json_mode_has_its_own_cache_and_preserves_reasoning_strength(self):
        for enabled in (False, True):
            client = self.client("high", jsonObjectMode=enabled)
            with patch.object(client, "_request", return_value=self.response()) as request:
                client.complete("JSON", {}, self.validate)
                self.assertEqual(request.call_count, 1)
                body = request.call_args.args[0]
                self.assertEqual(body["reasoning_effort"], "high")
                self.assertEqual(body.get("response_format"), {"type": "json_object"} if enabled else None)
                client.complete("JSON", {}, self.validate)
                self.assertEqual(request.call_count, 1)

    def test_invalid_or_misleading_deepseek_levels_fail_before_network(self):
        for effort in ("bad", "ultra", "", True, 4):
            with self.assertRaises(UserError):
                self.client(effort)
        for model in ("deepseek-v4.1-flash", "DeepSeek-V4-Pro", "provider/deepseek-flash"):
            with self.assertRaisesRegex(UserError, "没有独立的中档"):
                self.client("medium", model=model)
        self.assertEqual(self.client("medium").reasoning_effort, "medium")
        for effort in ("low", "high", "max", "default"):
            self.assertEqual(self.client(effort, model="deepseek-v4.1-flash").reasoning_effort, effort)

    def test_reasoning_only_is_not_saved_or_retried_as_successful_output(self):
        client = self.client("high")
        with patch.object(client, "_request", return_value=self.response("  ", reasoning="private-thought")) as request:
            with self.assertRaisesRegex(UserError, "没有整理正文"):
                client.complete("prompt", {"source": "text"}, self.validate)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(list(self.folder.glob("*.json")), [])
        self.assertNotIn("private-thought", (self.folder / "usage.jsonl").read_text())

    def test_truncated_output_is_not_saved(self):
        client = self.client("max", summaryVersion="reasoning-test")
        with patch.object(client, "_request", return_value=self.response(reason="length")):
            with self.assertRaises(OutputLimit):
                client.complete("prompt", {"source": "text"}, self.validate)
        self.assertEqual(list(self.folder.glob("*.json")), [])
        self.assertEqual(len(list((self.folder / "limits").glob("*.json"))), 1)

    def test_input_context_and_output_limits_remain_distinct_after_restart(self):
        for kind, expected in (("provider_context_limit", InputContextLimit), ("output_limit", ResponseOutputLimit)):
            directory = self.folder / kind
            config = {**self.config, "summaryVersion": "typed-limits"}
            client = ChatClient(config, directory, lambda *a: None)
            request = {"side_effect": InputContextLimit()} if kind == "provider_context_limit" else {"return_value": self.response(reason="length")}
            with patch.object(client, "_request", **request):
                with self.assertRaises(expected):
                    client.complete("prompt", {}, self.validate)
            resumed = ChatClient(config, directory, lambda *a: None)
            with patch.object(resumed, "_request", side_effect=AssertionError("must not repeat paid request")):
                with self.assertRaises(expected):
                    resumed.complete("prompt", {}, self.validate)

    def test_provider_context_error_has_input_class_without_retry(self):
        client = self.client("high", summaryVersion="typed-limits")
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(client.endpoint, 400, "Bad Request", {},
            io.BytesIO(b'{"error":{"code":"context_length_exceeded"}}'))
        with patch("summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(InputContextLimit):
                client.complete("prompt", {}, self.validate)
        self.assertEqual(opener.open.call_count, 1)

    def test_reasoning_exhaustion_stops_fallback_and_survives_restart(self):
        client = self.client("low", summaryVersion="reasoning-test")
        response = self.response("", reason="length", reasoning="private-thought")
        with patch.object(client, "_request", return_value=response) as request:
            with self.assertRaisesRegex(UserError, "不会继续自动拆分"):
                client.complete("prompt", {"source": "text"}, self.validate)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(list(self.folder.glob("*.json")), [])
        self.assertNotIn("private-thought", (self.folder / "usage.jsonl").read_text())
        resumed = self.client("low", summaryVersion="reasoning-test")
        with patch.object(resumed, "_request") as request:
            with self.assertRaisesRegex(UserError, "不会继续自动拆分"):
                resumed.complete("prompt", {"source": "text"}, self.validate)
        request.assert_not_called()

    def test_cancel_before_limit_marker_preserves_response_classification_without_new_request(self):
        for content, kind, expected in (("", "no_final_output", ReasoningOutputExhausted),
                                        ('{"private-final":', "output_limit", ResponseOutputLimit)):
            with self.subTest(kind=kind):
                directory = self.folder / kind
                config = {**self.config, "summaryVersion": "dialogue-reader-v2.0"}
                client = ChatClient(config, directory, lambda *args: None)
                def interrupted_marker(path, value):
                    if Path(path).parent.name == "limits": raise KeyboardInterrupt()
                    write_json(path, value)
                with patch.object(client, "_request", return_value=self.response(content, reason="length", reasoning="private-thought")) as network, patch("summary.write_json", side_effect=interrupted_marker):
                    with self.assertRaises(KeyboardInterrupt):
                        client.complete("prompt", {}, self.validate, output_tokens=131072)
                self.assertEqual(network.call_count, 1)
                self.assertEqual(list((directory / "limits").glob("*.json")), [])
                raw = (directory / "usage.jsonl").read_text()
                entry = json.loads(raw)
                self.assertEqual(entry["limitKind"], kind)
                self.assertEqual(entry["noFinalOutput"], content == "")
                self.assertNotIn("private-thought", raw)
                self.assertNotIn("private-final", raw)
                resumed = ChatClient(config, directory, lambda *args: None)
                with patch.object(resumed, "_request", side_effect=AssertionError("no repeat paid request")) as network:
                    with self.assertRaises(expected):
                        resumed.complete("prompt", {}, self.validate, output_tokens=131072)
                network.assert_not_called()
                self.assertEqual(resumed.request_count, 0)
                self.assertEqual(resumed.limit_hits, 1)

    def test_unknown_legacy_length_is_conservative_only_for_dialogue_reader(self):
        for version, expected in (("dialogue-reader-v2.0", ReasoningOutputExhausted), ("legacy-video", ResponseOutputLimit)):
            with self.subTest(version=version):
                directory = self.folder / version
                config = {**self.config, "summaryVersion": version}
                client = ChatClient(config, directory, lambda *args: None)
                with patch.object(client, "_request", return_value=self.response(reason="length")):
                    with self.assertRaises(ResponseOutputLimit): client.complete("prompt", {}, self.validate)
                marker = next((directory / "limits").glob("*.json")); marker.unlink()
                entry = json.loads((directory / "usage.jsonl").read_text())
                entry.pop("limitKind"); entry.pop("noFinalOutput")
                (directory / "usage.jsonl").write_text(json.dumps(entry) + "\n")
                resumed = ChatClient(config, directory, lambda *args: None)
                with patch.object(resumed, "_request") as network:
                    with self.assertRaises(expected) as error: resumed.complete("prompt", {}, self.validate)
                network.assert_not_called()
                if expected is ReasoningOutputExhausted: self.assertIn("无法确认是否包含正文", str(error.exception))

    def test_explicit_limit_marker_precedes_log_classification(self):
        client = self.client(summaryVersion="dialogue-reader-v2.0")
        with patch.object(client, "_request", return_value=self.response("", reason="length")):
            with self.assertRaises(ReasoningOutputExhausted): client.complete("prompt", {}, self.validate)
        marker = next((self.folder / "limits").glob("*.json"))
        write_json(marker, {"kind": "provider_context_limit"})
        resumed = self.client(summaryVersion="dialogue-reader-v2.0")
        with patch.object(resumed, "_request") as network:
            with self.assertRaises(InputContextLimit): resumed.complete("prompt", {}, self.validate)
        network.assert_not_called()

    def test_unsupported_parameter_error_does_not_silently_downgrade(self):
        client = self.client("max")
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            client.endpoint, 400, "Bad Request", {}, io.BytesIO(b'{"error":{"code":"unsupported_parameter"}}'))
        with patch("summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(UserError, "HTTP 400"):
                client.complete("prompt", {"source": "text"}, self.validate)
        self.assertEqual(opener.open.call_count, 1)
        body = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(body["reasoning_effort"], "max")

    def test_high_reasoning_waits_longer_but_timeout_is_never_automatically_retried(self):
        for effort, timeout in (("low", 600), ("high", 600), ("max", 600)):
            with self.subTest(effort=effort):
                client = self.client(effort, usageRoot=str(self.folder / effort))
                opener = Mock()
                opener.open.side_effect = TimeoutError("upstream still generating")
                with patch("summary.urllib.request.build_opener", return_value=opener), patch("summary.time.sleep") as sleep:
                    with self.assertRaisesRegex(UserError, "未自动重复提交"):
                        client.complete("prompt", {}, self.validate)
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(opener.open.call_args.kwargs["timeout"], timeout)
                sleep.assert_not_called()
                events = list((self.folder / effort / "usage/events").glob("*.json"))
                self.assertEqual(len(events), 1)
                event = json.loads(events[0].read_text())
                self.assertEqual(event["status"], "network_error")
                self.assertNotIn("inputTokens", event["usage"])

    def test_short_low_effort_request_keeps_short_wait(self):
        client = self.client("low")
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with patch("summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(UserError, "未自动重复提交"):
                client.complete("prompt", {}, self.validate, output_tokens=1000)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 150)

    def test_large_review_budget_allows_generation_time_without_duplicate_request(self):
        client = self.client("high", summaryTimeoutSeconds=7200)
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with patch("summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(UserError, "未自动重复提交"):
                client.complete("prompt", {}, self.validate, output_tokens=131072)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 1800)

    def test_wrapped_timeout_and_remaining_run_budget(self):
        client = self.client("high", summaryTimeoutSeconds=30)
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(TimeoutError("timed out"))
        with patch("summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(UserError, "未自动重复提交"):
                client.complete("prompt", {}, self.validate)
        self.assertEqual(opener.open.call_count, 1)
        self.assertGreater(opener.open.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 30)

    def test_cancelled_request_is_metered_as_unknown_without_retry(self):
        client = self.client("high", usageRoot=str(self.folder / "cancelled"))
        opener = Mock()
        opener.open.side_effect = KeyboardInterrupt()
        with patch("summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(KeyboardInterrupt):
                client.complete("prompt", {}, self.validate)
        self.assertEqual(opener.open.call_count, 1)
        events = list((self.folder / "cancelled/usage/events").glob("*.json"))
        self.assertEqual(len(events), 1)
        event = json.loads(events[0].read_text())
        self.assertEqual(event["status"], "cancelled")
        self.assertNotIn("outputTokens", event["usage"])


if __name__ == "__main__":
    unittest.main()
