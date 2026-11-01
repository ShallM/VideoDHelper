"""Normalize local ASR outputs without asking a language model to rewrite them.

Qwen's aligner removes punctuation before predicting timestamps. Its cleaned
text is never the transcript: original character spans remain authoritative.
"""
import contextlib
import gc
import importlib.metadata
import math
import os
from pathlib import Path
import re
import sys
import unicodedata

from common import UserError

QWEN_MAX_TOKENS = 8192
QWEN_MAX_AUDIO_SECONDS = 300


def adapter_version(engine):
    distribution = "mlx-audio" if engine == "mlx-audio" else "mlx-whisper"
    try:
        installed = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        installed = "not-installed"
    return f"{distribution}-normalized-v1/{installed}"


@contextlib.contextmanager
def offline_models():
    """Local managed resources must never trigger an implicit Hub download."""
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "1"
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def clear_model_cache():
    module = sys.modules.get("mlx_whisper.transcribe")
    holder = getattr(module, "ModelHolder", None)
    if holder is not None:
        holder.model = None
        holder.model_path = None
    gc.collect()
    mx = sys.modules.get("mlx.core")
    if mx is not None and hasattr(mx, "clear_cache"):
        mx.clear_cache()


def _kept(character):
    return character == "'" or unicodedata.category(character)[0] in ("L", "N")


def _cjk(character):
    code = ord(character)
    return any(lower <= code <= upper for lower, upper in (
        (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF),
        (0x2A700, 0x2B73F), (0x2B740, 0x2B81F), (0x2B820, 0x2CEAF), (0xF900, 0xFAFF)))


def alignment_text_spans(text):
    """Mirror the pinned Chinese aligner tokenizer while retaining raw offsets."""
    spans, start = [], None
    for index, character in enumerate(text):
        if _cjk(character) or not _kept(character):
            if start is not None:
                spans.append((start, index))
                start = None
            if _cjk(character):
                spans.append((index, index + 1))
        elif start is None:
            start = index
    if start is not None:
        spans.append((start, len(text)))
    return spans


def remap_alignment(text, items, duration):
    """Map CJK-character/Latin-token times back onto the exact ASR string.

    Separators belong to the preceding token (leading separators to the first).
    A timestamp can overshoot only by one 80 ms aligner bin at the audio end;
    that rounding is clipped, all larger or nonmonotonic values are rejected.
    """
    if not isinstance(text, str) or not math.isfinite(duration) or duration <= 0:
        raise UserError("时间对齐输入无效，识别检查点已保留。")
    spans = alignment_text_spans(text)
    if not spans:
        if not text.strip() and not items:
            return []
        raise UserError("识别结果没有可对齐的语音文字，原始识别结果已保留。")
    if len(spans) != len(items):
        raise UserError("时间对齐的文字数量与识别原文不一致，未覆盖原文，可继续重试对齐。")
    words, previous_start, previous_end = [], 0.0, 0.0
    for index, ((first, last), item) in enumerate(zip(spans, items)):
        value = item if isinstance(item, dict) else vars(item)
        if value.get("text") != text[first:last]:
            raise UserError("时间对齐的文字与识别原文不一致，原文已保留，不能使用清洗后的文字替代。")
        try:
            start, end = float(value["start_time"]), float(value["end_time"])
        except (ValueError, TypeError, KeyError):
            raise UserError("时间对齐返回了无效时间，识别检查点已保留。") from None
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
                or end > duration + 0.081 or start < previous_start or end < previous_end):
            raise UserError("时间对齐超出音频范围或顺序无效，识别检查点已保留。")
        previous_start, previous_end = start, end
        start, end = min(start, duration), min(end, duration)
        raw_first = 0 if index == 0 else first
        raw_last = spans[index + 1][0] if index + 1 < len(spans) else len(text)
        words.append({"word": text[raw_first:raw_last], "start": start, "end": end})
    if "".join(w["word"] for w in words) != text:
        raise UserError("识别原文映射不完整，未写入时间对齐结果。")
    return words


def readable_segments(words):
    """Group timed units for display without losing or inventing characters."""
    result, group = [], []
    def flush():
        if group:
            result.append({"text": "".join(w["word"] for w in group),
                           "start": group[0]["start"], "end": max(w["end"] for w in group),
                           "words": list(group)})
            group.clear()
    for word in words:
        if group and (word["start"] - group[-1]["end"] > 1.0
                      or len("".join(w["word"] for w in group)) >= 120
                      or word["end"] - group[0]["start"] > 30):
            flush()
        group.append(word)
        if re.search(r"[。！？!?；;\n][\s\"”’）)]*$", word["word"]):
            flush()
    flush()
    return result


def qwen_recognition_result(result, duration, max_tokens=QWEN_MAX_TOKENS):
    value = result if isinstance(result, dict) else getattr(result, "__dict__", {})
    text = value.get("text")
    generated, prompt = value.get("generation_tokens"), value.get("prompt_tokens")
    if (not isinstance(text, str) or type(generated) is not int or type(prompt) is not int
            or generated < 0 or prompt < 0):
        raise UserError("Qwen 识别结果不完整，未写入成功检查点。")
    if generated >= max_tokens:
        raise UserError("Qwen 当前片段已达到输出上限，可能缺少尾部；已保留其他片段，请换用其他模型新建任务。")
    # We submit one bounded audio window. Chunk metadata is used only to detect
    # incomplete processing, never as a word/segment alignment substitute.
    chunks = value.get("segments")
    if not isinstance(chunks, list) or len(chunks) != 1 or not isinstance(chunks[0], dict):
        raise UserError("Qwen 未完整处理当前音频片段，未写入成功检查点。")
    if chunks[0].get("text") != text:
        raise UserError("Qwen 片段文字与识别正文不一致，未写入成功检查点。")
    try:
        start, end = float(chunks[0]["start"]), float(chunks[0]["end"])
        covered = math.isfinite(start) and math.isfinite(end) and abs(start) <= 0.001 and abs(end - duration) <= 0.02
    except (TypeError, ValueError, KeyError):
        covered = False
    if not covered:
        raise UserError("Qwen 音频覆盖范围不完整，已保留其他片段，不能将缺尾结果当作完成。")
    return {"text": text, "localPromptTokens": prompt, "localGenerationTokens": generated,
            "audioSeconds": duration}


class QwenAdapter:
    def __init__(self, paths):
        self.paths = paths
        self.model = None
        self.component = None

    def _load(self, component):
        if self.component != component:
            self.unload()
            path = Path(self.paths[component]).resolve()
            if not path.is_dir():
                raise UserError("本地 Qwen 模型组件缺失，请在设置中修复下载。")
            from mlx_audio.stt.utils import load_model
            # Passing a Path skips Hub resolution. The pinned manifest contains
            # tokenizer/config assets, and offline mode also guards hooks.
            with offline_models(), contextlib.redirect_stdout(sys.stderr):
                self.model = load_model(path, lazy=False, strict=True)
            self.component = component
        return self.model

    def recognize(self, wave, duration):
        if not 0 < duration <= QWEN_MAX_AUDIO_SECONDS:
            raise UserError("Qwen 音频片段超过五分钟限制，未开始识别。")
        model = self._load("asr")
        with offline_models(), contextlib.redirect_stdout(sys.stderr):
            result = model.generate(str(wave), language="Chinese", max_tokens=QWEN_MAX_TOKENS,
                                    temperature=0.0, batch_size=1, chunk_duration=301.0,
                                    min_chunk_duration=0.0, stream=False, verbose=False)
        return qwen_recognition_result(result, duration)

    def align(self, wave, text, duration):
        if not text.strip():
            return []
        if not alignment_text_spans(text):
            return remap_alignment(text, [], duration)
        if not 0 < duration <= QWEN_MAX_AUDIO_SECONDS:
            raise UserError("时间对齐片段超过五分钟限制，识别检查点已保留。")
        model = self._load("aligner")
        with offline_models(), contextlib.redirect_stdout(sys.stderr):
            result = model.generate(str(wave), text=text, language="Chinese")
        return readable_segments(remap_alignment(text, result.items, duration))

    def unload(self):
        self.model = None
        self.component = None
        clear_model_cache()
