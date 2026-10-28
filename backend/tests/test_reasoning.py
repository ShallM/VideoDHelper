import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from common import UserError
from summary import ChatClient, OutputLimit, reasoning_effort


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


if __name__ == "__main__":
    unittest.main()
