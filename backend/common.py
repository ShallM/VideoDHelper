"""Private, atomic task storage. No credentials belong in a task or event."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


class UserError(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_segments(segments):
    if not isinstance(segments, list) or not segments:
        raise UserError("没有可处理的语音文字，请选择其他字幕或本地转录。")
    for index, segment in enumerate(segments):
        if (not isinstance(segment, dict) or type(segment.get("id")) is not int or segment.get("id") != index or not isinstance(segment.get("text"), str)
                or not segment["text"].strip()
                or any(type(segment.get(k)) not in (int, float) or not math.isfinite(segment[k]) for k in ("start", "end"))
                or not 0 <= segment["start"] <= segment["end"]
                or (index > 0 and segment["start"] < segments[index - 1]["start"])):
            raise UserError("原稿的编号、时间或文字无效，不能继续生成笔记。")
        if segment.get("speaker") is not None and segment["speaker"] not in ("A", "B", "unknown"):
            raise UserError("原稿的说话人编号无效，请重新检查修订。")
        if "words" in segment:
            if not isinstance(segment["words"], list):
                raise UserError("原稿的词语时间无效。")
            previous = segment["start"]
            for word in segment["words"]:
                if (not isinstance(word, dict) or not isinstance(word.get("word"), str)
                        or any(type(word.get(k)) not in (int, float) or not math.isfinite(word[k]) for k in ("start", "end"))
                        or not segment["start"] <= word["start"] <= word["end"] <= segment["end"] + 0.001
                        or word["start"] < previous):
                    raise UserError("原稿的词语时间无效。")
                previous = word["start"]


def stamp(seconds, milliseconds=False):
    value = round(seconds * 1000)
    base = f"{value // 3600000:02d}:{value // 60000 % 60:02d}:{value // 1000 % 60:02d}"
    return base + (f",{value % 1000:03d}" if milliseconds else "")


def save_transcript(folder, segments):
    validate_segments(segments)
    write_json(folder / "transcript.json", segments)
    write_text(folder / "transcript.txt", "\n\n".join(f'[{stamp(s["start"])}] {s["text"]}' for s in segments) + "\n")
    write_text(folder / "source.srt", "\n\n".join(
        f'{i + 1}\n{stamp(s["start"], True)} --> {stamp(s["end"], True)}\n{s["text"]}'
        for i, s in enumerate(segments)) + "\n")


def clean_media(folder):
    """Only the two dedicated directories of this job; never follow symlinks."""
    import shutil
    folder = Path(folder).resolve()
    for name in ("media", "temporary"):
        target = folder / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
