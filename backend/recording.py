"""Local recordings: immutable audio/ASR, optional diarization, explicit AI stage."""
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from common import UserError, digest, read_json, save_transcript, stamp, validate_segments, write_json, write_text
from media import executable, release_transcription_model, transcribe

SPEAKERS = {"A": "说话人 A", "B": "说话人 B", "unknown": "未确定"}
VERSION = "recording-v1"


def source_file(folder, relative):
    """Never read an external path or follow a symlink supplied in a request."""
    if not isinstance(relative, str):
        raise UserError("录音原件路径无效，请重新导入。")
    path = Path(relative)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "source" or any(p in (".", "..") for p in path.parts):
        raise UserError("录音原件必须位于本任务的 source 目录。")
    directory, target = Path(folder) / "source", Path(folder) / path
    if directory.is_symlink() or target.is_symlink() or not target.is_file() or target.resolve().parent != directory.resolve():
        raise UserError("录音原件不存在或路径不安全，请重新导入。")
    if target.suffix.lower() not in (".m4a", ".mp3", ".wav", ".aac", ".mp4", ".caf", ".flac", ".aiff", ".aif"):
        raise UserError("暂不支持这种录音格式，请导入 M4A、MP3 或 WAV。")
    return target


def file_digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def probe_audio(path):
    try:
        result = subprocess.run([executable("ffprobe"), "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type,codec_name,sample_rate,channels:format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=30)
        value = json.loads(result.stdout)
        duration = float(value["format"]["duration"])
        streams = value["streams"]
        if result.returncode or not streams or streams[0].get("codec_type") != "audio" or not math.isfinite(duration) or not 0 < duration <= 3002:
            raise ValueError()
        return {"duration": duration, "codec": streams[0].get("codec_name"), "channels": streams[0].get("channels"),
                "sampleRate": streams[0].get("sample_rate")}
    except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise UserError("无法读取有效音轨，或录音超过首版支持的 50 分钟；请检查文件。") from None


def validate_turns(value, duration):
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("turns"), list) or not value["turns"]:
        raise UserError("说话人分析没有返回有效结果；原稿已保留，可关闭区分说话人后重试。")
    previous = -1
    for turn in value["turns"]:
        if (not isinstance(turn, dict) or not isinstance(turn.get("speaker"), str) or turn.get("speaker") not in SPEAKERS
                or any(type(turn.get(k)) not in (int, float) or not math.isfinite(turn[k]) for k in ("start", "end"))
                or not 0 <= turn["start"] < turn["end"] <= duration + 1 or turn["start"] < previous):
            raise UserError("说话人分析的时间或编号无效；原稿已保留，可关闭区分说话人后重试。")
        previous = turn["start"]
    return value


def helper_path():
    override = os.environ.get("VIDEOHELPER_DIARIZATION_HELPER")
    candidates = [Path(override)] if override else []
    if directory := os.environ.get("VIDEOHELPER_BUNDLED_TOOLS"):
        candidates.append(Path(directory) / "recording-diarize")
    candidates.append(Path(__file__).resolve().parent.parent / "recording-diarize")
    candidates.append(Path(sys.executable).parent / "recording-diarize")
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise UserError("尚未安装本地说话人组件；原稿已保留，可关闭“区分两位说话人”后继续。")


def invoke_helper(command, emit):
    """Stream bounded progress, reap children on every cancellation/error path."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    events = queue.Queue(maxsize=100)
    def read_progress():
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line[:16384])
                    if isinstance(value, dict) and value.get("kind") == "progress":
                        try: events.put_nowait(value)
                        except queue.Full: pass
                except ValueError:
                    pass
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=read_progress, daemon=True)
    reader.start()
    deadline = time.monotonic() + 3600
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                raise UserError("说话人分析超时；原稿已保留，可重试或关闭区分说话人。")
            try:
                event = events.get(timeout=0.2)
                progress = event.get("fraction")
                if type(progress) not in (int, float) or not math.isfinite(progress): progress = None
                emit("diarize", str(event.get("message") or "正在分析说话人…")[:250],
                     min(1, max(0, progress)) if progress is not None else None)
            except queue.Empty:
                pass
        if process.returncode:
            raise UserError("本地说话人分析失败；已保留转录原稿。可继续重试，或关闭区分说话人后继续。")
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        reader.join(timeout=2)
        process.stdout.close()


def diarize(path, folder, support, manifest, emit):
    helper = helper_path()
    identity = digest({"audio": manifest["sha256"], "version": VERSION, "speakers": 2,
                       "helper": file_digest(helper)})
    checkpoint = folder / "checkpoints" / "diarization" / f"{identity}.json"
    if checkpoint.exists():
        emit("diarize", "复用已完成的整段说话人分析。", 1)
        value = validate_turns(read_json(checkpoint), manifest["duration"])
        write_json(folder / "diarization.json", value)
        return value
    temporary = folder / "temporary"
    temporary.mkdir(mode=0o700, exist_ok=True)
    wave, output = temporary / "diarization.wav", temporary / "diarization-result.json"
    output.unlink(missing_ok=True)
    emit("diarize", "准备两位说话人分析；首次使用将准备本地模型…")
    try:
        result = subprocess.run([executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(path),
            "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wave)], capture_output=True, timeout=180)
        if result.returncode:
            raise UserError("说话人分析的音轨转换失败；转录原稿已保留。")
        invoke_helper([str(helper), "--input", str(wave), "--output", str(output), "--speakers", "2",
                       "--cache-dir", str(support / "models" / "speakerkit")], emit)
        value = validate_turns(read_json(output), manifest["duration"])
        write_json(checkpoint, value)
        write_json(folder / "diarization.json", value)
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise UserError("说话人组件未完成有效分析；原稿已保留，可关闭区分说话人后重试。") from None
    finally:
        wave.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def speaker_at(start, end, turns):
    """Use acoustics only. Simultaneous/ambiguous evidence remains unknown."""
    if end <= start:
        active = {t["speaker"] for t in turns if t["start"] <= start < t["end"]}
        return active.pop() if len(active) == 1 else "unknown"
    scores = {}
    for turn in turns:
        overlap = max(0, min(end, turn["end"]) - max(start, turn["start"]))
        if overlap:
            scores[turn["speaker"]] = scores.get(turn["speaker"], 0) + overlap
    # Conservative: do not assign words straddling a speaker change or overlap.
    if len(scores) != 1:
        return "unknown"
    speaker, overlap = next(iter(scores.items()))
    return speaker if overlap / (end - start) >= 0.5 else "unknown"


def align_speakers(segments, turns):
    result = []
    for segment in segments:
        words = segment.get("words")
        if not words:
            result.append({**segment, "speaker": speaker_at(segment["start"], segment["end"], turns)})
            continue
        groups = []
        for word in words:
            speaker = speaker_at(word["start"], word["end"], turns)
            if not groups or groups[-1][0] != speaker:
                groups.append((speaker, []))
            groups[-1][1].append(word)
        for speaker, group in groups:
            content = "".join(word["word"] for word in group).strip()
            if content:
                result.append({"start": group[0]["start"], "end": group[-1]["end"], "text": content,
                               "speaker": speaker, "words": group})
    result = [{**segment, "id": index} for index, segment in enumerate(result)]
    validate_segments(result)
    return result


def validate_speakers(value):
    if (not isinstance(value, dict) or any(k not in SPEAKERS or not isinstance(v, str) or not v.strip() or len(v) > 80 for k, v in value.items())):
        raise UserError("说话人显示名称无效，请检查修订。")
    return {**SPEAKERS, **value}


def effective_transcript(folder):
    segments = read_json(folder / "transcript.json")
    validate_segments(segments)
    names = dict(SPEAKERS)
    edit = folder / "transcript-edits.json"
    if edit.exists():
        value = read_json(edit)
        try:
            if value.get("version") != 1 or str(uuid.UUID(value["revision"])).lower() != value["revision"].lower(): raise ValueError()
            segments = value["segments"]
            validate_segments(segments)
            names = validate_speakers(value["speakers"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UserError("人工修订文件无效；原始转录仍保留，请检查修订后再整理。") from None
    # A rename changes only this display mapping, including after a text edit.
    if (folder / "speakers.json").exists():
        names = validate_speakers(read_json(folder / "speakers.json"))
    # Names are presentation only; stable IDs/text/timing determine AI identity.
    normalized = [{k: segment.get(k, "unknown" if k == "speaker" else None) for k in ("id", "start", "end", "text", "speaker")} for segment in segments]
    return segments, names, digest(normalized)


def prepare_transcript(request, folder, support, emit):
    path = source_file(folder, request.get("localAudioFile"))
    emit("prepare", "检查本地录音原件…")
    sha = file_digest(path)
    manifest_path = folder / "recording-source.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("sha256") != sha:
            raise UserError("任务内录音原件已变化，请新建录音任务，避免将旧原稿匹配到新音频。")
    else:
        manifest = {"version": 1, "path": request["localAudioFile"], "sha256": sha, "size": path.stat().st_size, **probe_audio(path)}
        write_json(manifest_path, manifest)
    asr_path = folder / "asr-transcript.json"
    if asr_path.exists():
        segments = read_json(asr_path)
        validate_segments(segments)
        emit("transcript", "复用已完成的本地转录，原音频与词语时间保留。")
    else:
        try:
            segments = transcribe(path, request.get("whisperModel", "turbo"), folder, support, emit, preserve_word_timing=True)
        finally:
            release_transcription_model()
        validate_segments(segments)
        write_json(asr_path, segments)
    # A failed optional stage must still leave readable/exportable raw text.
    if not (folder / "transcript.json").exists():
        save_transcript(folder, [{**s, "speaker": "unknown"} for s in segments])
    mode = request.get("diarizationMode", "off")
    identity = digest({"asr": segments, "mode": mode, "version": VERSION})
    prepared = folder / "recording-transcript.json"
    if prepared.exists() and read_json(prepared).get("identity") == identity:
        return effective_transcript(folder)[0], manifest
    if (folder / "transcript-edits.json").exists():
        raise UserError("已有人工修订，不能重新覆盖说话人分段。请保留修订，直接整理当前对话。")
    if mode == "two":
        value = diarize(path, folder, support, manifest, emit)
        segments = align_speakers(segments, value["turns"])
    else:
        segments = [{**s, "speaker": "unknown"} for s in segments]
    save_transcript(folder, segments)
    if not (folder / "speakers.json").exists():
        write_json(folder / "speakers.json", SPEAKERS)
    write_json(prepared, {"version": 1, "identity": identity, "diarizationMode": mode})
    return segments, manifest


def display_text(value, names):
    return re.sub(r"\[(A|B|unknown)\]", lambda m: names.get(m[1], m[1]), value)


def export_dialogue(folder, title, segments, names, notes=None, condensed=None):
    def refs(ids):
        return "、".join(stamp(segments[i]["start"]) for i in ids)
    raw = "\n\n".join(f'[{stamp(s["start"])}] {names.get(s.get("speaker", "unknown"), "未确定")}：{s["text"]}' for s in segments)
    write_text(folder / "dialogue-transcript.txt", raw + "\n")
    if notes is None: return
    lines = [f"# {title}", "来源：本地录音", f"整理模型：{notes['model']}", "## 对话概览", display_text(notes["overview"], names)]
    for chapter in notes["chapters"]:
        lines += [f"## {chapter['title']}"]
        for claim in chapter.get("claims", []):
            lines += [display_text(claim["text"], names), f"录音时间：{refs(claim['source_ids'])}"]
    for label, key in (("主要观点与共识", "key_points"), ("分歧与待核对", "uncertainties")):
        lines += [f"## {label}"] + [f"- {display_text(item['text'], names)}（{refs(item['source_ids'])}）" for item in notes[key]]
    write_text(folder / "study-notes.md", "\n\n".join(lines) + "\n")
    write_text(folder / "condensed.md", f"# {title} · 整理对话\n\n" + "\n\n".join(
        f"[{refs(item['source_ids'])}] {names.get(item.get('speaker', 'unknown'), '未确定')}：{display_text(item['text'], names)}" for item in condensed) + "\n")


def run_recording(request, record, folder, support, emit, load_config, env_file=None):
    if request.get("recordingAction", "transcribe") == "transcribe":
        segments, manifest = prepare_transcript(request, folder, support, emit)
        segments, names, revision = effective_transcript(folder)
        export_dialogue(folder, request["title"], segments, names)
        (folder / "temporary" / "window.wav").unlink(missing_ok=True)
        record.update(state="transcribed", stage="transcribed", progress=1.0, updatedAt=time.time(),
            message="录音原稿已保存；请核对文字与说话人，再点击整理对话。尚未调用大语言模型。",
            segmentCount=len(segments), duration=manifest["duration"], mediaFiles=[request["localAudioFile"]], sourceKind="recording")
    else:
        if not (folder / "transcript.json").exists():
            raise UserError("请先完成本地转录，再生成整理与总结。")
        segments, names, revision = effective_transcript(folder)
        config = load_config(support, env_file)
        config.update(taskID=record["id"], taskTitle=request["title"], sourceKind="recording", summaryStrategy="structured",
                      summaryReview=request.get("summaryReview", True), sourceRevision=revision,
                      recordingRevision=read_json(folder / "transcript-edits.json")["revision"] if (folder / "transcript-edits.json").exists() else "original")
        from summary_v2 import summarize
        notes, condensed = summarize(segments, request["title"], config, folder, emit)
        export_dialogue(folder, request["title"], segments, names, notes, condensed)
        record.update(state="completed", stage="completed", progress=1.0, updatedAt=time.time(),
            message="对话原稿、整理对话与总结已保存。", segmentCount=len(segments), sourceKind="recording", summarySourceRevision=revision)
    write_json(folder / "job.json", record)
    print(json.dumps({"stage": record["stage"], "message": record["message"], "progress": 1}, ensure_ascii=False), flush=True)
    return 0
