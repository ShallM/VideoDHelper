import copy
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import patch

from asr_adapters import (QwenAdapter, alignment_text_spans, readable_segments, remap_alignment,
                          qwen_recognition_result, QWEN_MAX_TOKENS)
from common import UserError, digest, read_json, validate_segments, write_json
from media import _freeze_model, _transcribe_managed, owned_words, _wave_has_signal


def aligned_items(text, start=0.2, step=0.2):
    return [{"text": text[first:last], "start_time": start + i * step,
             "end_time": start + (i + 1) * step}
            for i, (first, last) in enumerate(alignment_text_spans(text))]


class TextAlignmentTests(unittest.TestCase):
    def test_digital_silence_guard_preserves_even_one_quiet_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "silence.wav"
            def save(data):
                with wave.open(str(path), "wb") as stream:
                    stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                    stream.writeframes(data)
            for samples in (480, 16000):
                save(bytes(samples * 2))
                self.assertFalse(_wave_has_signal(path))
            save(bytes(32000) + b"\x01\x00")
            self.assertTrue(_wave_has_signal(path))

    def test_chinese_english_decimal_case_spaces_and_script_are_exactly_preserved(self):
        text = "  「繁體測試」，GPT-4.1 的學習率 3e-4；don't 改！\n 下一句。  "
        words = remap_alignment(text, aligned_items(text), 30)
        self.assertEqual("".join(w["word"] for w in words), text)
        self.assertIn("繁", [v["text"] for v in aligned_items(text)])
        self.assertIn("GPT", [v["text"] for v in aligned_items(text)])
        segments = readable_segments(words)
        self.assertEqual("".join(s["text"] for s in segments), text)
        validate_segments([dict(s, id=i) for i, s in enumerate(segments)])
        self.assertLess(len(segments), len(words))

    def test_combining_marks_and_leading_punctuation_are_not_rewritten(self):
        text = "（e\u0301） 正確。"
        self.assertEqual("".join(w["word"] for w in remap_alignment(text, aligned_items(text), 10)), text)

    def test_omitted_changed_or_extra_tokens_are_rejected(self):
        for items in (aligned_items("你好")[:1], aligned_items("你号"), aligned_items("你好啊")):
            with self.assertRaises(UserError):
                remap_alignment("你好", items, 10)

    def test_time_boundaries_and_monotonicity_are_enforced(self):
        for start, end in ((-1, 2), (0, float("nan")), (1, 20), (2, 1)):
            with self.assertRaises(UserError):
                remap_alignment("字", [{"text": "字", "start_time": start, "end_time": end}], 10)
        items = aligned_items("你好")
        items[1]["start_time"] = 0.1
        with self.assertRaises(UserError):
            remap_alignment("你好", items, 10)
        value = remap_alignment("字。", [{"text": "字", "start_time": 0.9, "end_time": 1.04}], 1)
        self.assertEqual(value[0]["end"], 1)

    def test_empty_speech_and_punctuation_only_are_distinguished(self):
        self.assertEqual(remap_alignment("  ", [], 1), [])
        with self.assertRaises(UserError):
            remap_alignment("……", [], 1)

    def test_overlap_ownership_preserves_delimiters_and_zero_length_last_word(self):
        words = [{"word": "前， ", "start": 7.7, "end": 7.9},
                 {"word": "後。 ", "start": 8.1, "end": 8.3},
                 {"word": "終！", "start": 16, "end": 16}]
        raw = {"segments": readable_segments(words)}
        left = owned_words(raw, 0, 0, 8, preserve_whitespace=True)
        right = owned_words(raw, 0, 8, 16, preserve_whitespace=True, include_endpoint=True)
        self.assertEqual("".join(s["text"] for s in left + right), "前， 後。 終！")
        self.assertEqual(right[-1]["words"][-1]["start"], 16)

    def test_generation_budget_or_missing_tail_is_not_success(self):
        good = {"text": "测试", "generation_tokens": 2, "prompt_tokens": 50,
                "segments": [{"text": "测试", "start": 0, "end": 20}]}
        self.assertEqual(qwen_recognition_result(good, 20)["text"], "测试")
        bad = copy.deepcopy(good); bad["generation_tokens"] = QWEN_MAX_TOKENS
        with self.assertRaisesRegex(UserError, "上限"):
            qwen_recognition_result(bad, 20)
        bad = copy.deepcopy(good); bad["segments"][0]["end"] = 10
        with self.assertRaisesRegex(UserError, "覆盖"):
            qwen_recognition_result(bad, 20)
        bad = copy.deepcopy(good); bad["segments"][0]["text"] = "部分"
        with self.assertRaises(UserError):
            qwen_recognition_result(bad, 20)

    def test_qwen_aligner_is_not_loaded_for_empty_or_invalid_text(self):
        adapter = QwenAdapter({"asr": "/missing", "aligner": "/missing"})
        with patch.object(adapter, "_load", side_effect=AssertionError("must not load")):
            self.assertEqual(adapter.align("unused", "", 2), [])
            with self.assertRaises(UserError):
                adapter.align("unused", "。", 2)
            with self.assertRaises(UserError):
                adapter.recognize("unused", 301)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.resource = {"modelID": "qwen3-asr-1.7b", "engine": "mlx-audio", "format": "bf16", "components": [
            {"id": "asr", "revision": "fixed-asr", "fingerprint": "one"},
            {"id": "aligner", "revision": "fixed-aligner", "fingerprint": "two"}]}

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self, **kwargs):
        with patch("media.adapter_version", return_value="test-v1"):
            return _freeze_model(self.root, kwargs.get("name", "qwen3-asr-1.7b"), kwargs.get("resource", self.resource),
                                 "audio-hash", True, kwargs.get("legacy", False))

    def test_model_and_component_versions_are_frozen(self):
        original = self.freeze()
        mtime = (self.root / "transcription-model.json").stat().st_mtime_ns
        self.assertEqual(self.freeze(), original)
        self.assertEqual((self.root / "transcription-model.json").stat().st_mtime_ns, mtime)
        changed = copy.deepcopy(self.resource); changed["components"][1]["revision"] = "changed"
        with self.assertRaises(UserError):
            self.freeze(resource=changed)
        with self.assertRaises(UserError):
            self.freeze(name="turbo")

    def test_legacy_windows_are_reused_only_for_legacy_task(self):
        identity = digest({"sha256": "audio-hash", "model": "turbo", "version": 3})
        (self.root / "checkpoints" / "whisper" / identity).mkdir(parents=True)
        resource = {"modelID": "turbo", "engine": "mlx-whisper", "components": []}
        value = self.freeze(name="turbo", resource=resource, legacy=True)
        self.assertEqual(value["checkpointKind"], "whisper")
        self.assertEqual(value["checkpointIdentity"], identity)
        (self.root / "transcription-model.json").unlink()
        value = self.freeze(name="turbo", resource=resource, legacy=False)
        self.assertEqual(value["checkpointKind"], "asr")
        self.assertNotEqual(value["checkpointIdentity"], identity)

    def test_qwen_alignment_failure_and_resume_never_redo_completed_recognition(self):
        snapshot = self.freeze()
        installed = {"snapshot": self.resource, "paths": {"asr": "/unused", "aligner": "/unused"}}
        calls = {"recognition": 0, "alignment": 0, "fail": True}
        class FakeAdapter:
            def __init__(self, paths): pass
            def unload(self): pass
            def recognize(self, wave, seconds):
                calls["recognition"] += 1
                return {"text": "测试。", "localPromptTokens": 50, "localGenerationTokens": 2, "audioSeconds": seconds}
            def align(self, wave, text, seconds):
                calls["alignment"] += 1
                if calls["fail"] and calls["alignment"] == 2:
                    raise UserError("alignment failed")
                return readable_segments(remap_alignment(text, aligned_items(text, start=10), seconds))
        mx = SimpleNamespace(default_device=lambda: "gpu", get_peak_memory=lambda: 123)
        plan = {"boundaries": [0., 240., 480.], "snappedBoundaries": 0, "fallbackBoundaries": 1}
        def run():
            return _transcribe_managed(Path("audio"), "qwen3-asr-1.7b", self.root, lambda *a: None, 480., installed, snapshot, mx)
        with patch("media.QwenAdapter", FakeAdapter), patch("media.plan_windows", return_value=plan), patch("media._extract_wave", return_value=True):
            with self.assertRaisesRegex(UserError, "alignment failed"):
                run()
            self.assertEqual(calls["recognition"], 2)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.glob("checkpoints/asr/*/recognition/*.json")}
            calls["fail"] = False
            result = run()
            self.assertEqual(calls, {"recognition": 2, "alignment": 3, "fail": False})
            self.assertEqual("".join(s["text"] for s in result), "测试。测试。")
            for p, state in before.items():
                self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), state)
            self.assertEqual(read_json(self.root / "transcription-runtime.json")["localGenerationTokens"], 0)
            self.assertEqual(read_json(self.root / "transcription-runtime.json")["reusedAlignmentWindows"], 1)
            run()
            self.assertEqual(calls["alignment"], 3)
            self.assertTrue(read_json(self.root / "transcription-runtime.json")["completed"])

    def test_whisper_variants_share_adapter_and_resume_without_loading_model(self):
        calls = []
        def recognize(*args, **kwargs):
            calls.append(kwargs)
            return {"segments": [{"start": 0.1, "end": 0.7, "text": "测试。", "words": [
                {"word": "测试。", "start": 0.1, "end": 0.7}]}]}
        module = SimpleNamespace(transcribe=recognize)
        mx = SimpleNamespace(default_device=lambda: "gpu", get_peak_memory=lambda: 123)
        plan = {"boundaries": [0., 1.], "snappedBoundaries": 0, "fallbackBoundaries": 0}
        with patch.dict(sys.modules, {"mlx_whisper": module}), patch("media.plan_windows", return_value=plan), patch("media._extract_wave", return_value=True):
            for name in ("turbo", "large-v3"):
                folder = self.root / name; folder.mkdir()
                resources = {"modelID": name, "engine": "mlx-whisper", "components": []}
                snapshot = _freeze_model(folder, name, resources, "audio", True, False)
                installed = {"snapshot": resources, "paths": {"asr": "/models/" + name}}
                for _ in range(2):
                    result = _transcribe_managed(Path("audio"), name, folder, lambda *a: None, 1., installed, snapshot, mx)
                    self.assertEqual(result[0]["text"], "测试。")
                    self.assertEqual(result[0]["id"], 0)
                self.assertEqual(read_json(folder / "transcription-runtime.json")["reusedWindows"], 1)
        self.assertEqual([c["path_or_hf_repo"] for c in calls], ["/models/turbo", "/models/large-v3"])
        self.assertTrue(all(c["word_timestamps"] and not c["condition_on_previous_text"] for c in calls))

    def test_qwen_overlong_window_does_not_start_model(self):
        snapshot = self.freeze()
        installed = {"snapshot": self.resource, "paths": {"asr": "/unused", "aligner": "/unused"}}
        mx = SimpleNamespace(default_device=lambda: "gpu")
        plan = {"boundaries": [0., 310.], "snappedBoundaries": 0, "fallbackBoundaries": 0}
        with patch("media.plan_windows", return_value=plan), patch("media._extract_wave", side_effect=AssertionError("must reject before decode")):
            with self.assertRaisesRegex(UserError, "范围"):
                _transcribe_managed(Path("audio"), "qwen3-asr-1.7b", self.root, lambda *a: None, 310., installed, snapshot, mx)

    def test_digital_silence_never_calls_either_recognizer(self):
        plan = {"boundaries": [0., 1.], "snappedBoundaries": 0, "fallbackBoundaries": 0}
        mx = SimpleNamespace(default_device=lambda: "gpu", get_peak_memory=lambda: 0)
        for name, engine in (("turbo", "mlx-whisper"), ("large-v3", "mlx-whisper"), ("qwen3-asr-1.7b", "mlx-audio")):
            folder = self.root / name; folder.mkdir()
            resource = {"modelID": name, "engine": engine, "components": []}
            snapshot = _freeze_model(folder, name, resource, "audio", True, False)
            with patch("media._extract_wave", return_value=False), patch("media.plan_windows", return_value=plan), \
                 patch.object(QwenAdapter, "recognize", side_effect=AssertionError("must skip silent ASR")), \
                 patch.object(QwenAdapter, "align", side_effect=AssertionError("must skip silent alignment")):
                with self.assertRaisesRegex(UserError, "没有识别到有效语音"):
                    _transcribe_managed(Path("audio"), name, folder, lambda *a: None, 1.,
                        {"snapshot": resource, "paths": {}}, snapshot, mx)


if __name__ == "__main__":
    unittest.main()
