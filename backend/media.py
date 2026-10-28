"""yt-dlp acquisition and bounded-memory, managed local ASR transcription."""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from common import UserError, digest, read_json, validate_segments, write_json
from asr_adapters import QwenAdapter, adapter_version, clear_model_cache, offline_models


def executable(name):
    if directory := os.environ.get("VIDEOHELPER_BUNDLED_TOOLS"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise UserError(f"应用内的 {name} 不完整，请重新安装 VideoDHelper。")
    path = shutil.which(name)
    if path:
        return path
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        path = str(Path(prefix) / name)
        if os.access(path, os.X_OK):
            return path
    raise UserError(f"未找到 {name}，请运行项目的依赖安装脚本。")


class QuietLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def download(request, folder, support, emit):
    import yt_dlp
    mode = request["mediaMode"]
    if mode == "needed" and not request["useWhisper"]:
        return None
    video = mode == "video"
    audio_id = request.get("audioFormatID", "")
    video_id = request.get("videoFormatID", "")
    for value in (audio_id, video_id):
        if value and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise UserError("媒体格式编号无效，请重新读取视频信息。")
    if video and not video_id:
        raise UserError("请选择该视频实际可用的画质。")
    selector = (video_id if request.get("videoIncludesAudio") else f"{video_id}+{audio_id or 'bestaudio'}") if video else (audio_id or "bestaudio/best")
    container = request.get("container", "mp4")
    if container not in ("mp4", "mkv"):
        raise UserError("封装格式无效。")
    identity = digest({"url": request["url"], "selector": selector, "container": container})
    manifest = folder / "media-manifest.json"
    if manifest.exists():
        saved = read_json(manifest)
        path = folder / saved["path"]
        if (saved.get("identity") == identity and path.resolve().parent == (folder / "media").resolve()
                and path.is_file() and not path.is_symlink() and path.stat().st_size == saved.get("size", -1)):
            emit("download", "复用本任务已下载的媒体。", 1)
            return path
    directory = folder / "media"
    directory.mkdir(exist_ok=True, mode=0o700)
    last = [0.0]

    def progress(data):
        now = time.monotonic()
        if now - last[0] < 0.5 and data.get("status") != "finished":
            return
        last[0] = now
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        done = data.get("downloaded_bytes", 0)
        percent = min(1, done / total) if total else None
        message = "下载媒体" + (f" · {percent:.0%}" if percent is not None else "…")
        if data.get("speed"):
            message += f" · {data['speed'] / 1024 / 1024:.1f} MB/s"
        emit("download", message, percent)

    options = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "noplaylist": True,
               "format": selector, "outtmpl": str(directory / "source.%(ext)s"),
               "merge_output_format": container, "ffmpeg_location": executable("ffmpeg"),
               "retries": 2, "fragment_retries": 2, "socket_timeout": 20,
               "continuedl": True, "progress_hooks": [progress],
               "postprocessor_hooks": [lambda _: emit("download", "正在合并音视频…")]}
    cookies = support / "bilibili-cookies.txt"
    if cookies.is_file():
        options["cookiefile"] = str(cookies)
    emit("download", "正在获取视频音轨…" if not video else "正在获取所选视频规格…")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(request["url"], download=True)
            if not info or info.get("_type") in ("playlist", "multi_video"):
                raise UserError("当前只处理指定的单个视频或分 P。")
            path = Path(info.get("filepath") or ydl.prepare_filename(info))
            # The final merged extension may differ from the extraction template.
            if video and not path.is_file():
                path = directory / ("source." + container)
            if not path.is_file() or path.is_symlink() or path.resolve().parent != directory.resolve():
                raise UserError("下载结束后没有找到完整媒体，可继续处理以重试。")
            write_json(manifest, {"identity": identity, "path": str(path.relative_to(folder)),
                                 "format": info.get("format"), "size": path.stat().st_size})
            return path
    except yt_dlp.utils.DownloadError:
        raise UserError("媒体获取失败。请检查网络、B 站登录状态及所选规格是否仍可用；已保留下载进度，可继续重试。") from None


def prepare_model(name, support, emit):
    """Compatibility lookup only; downloads are explicit model-manager actions.

    Actual inference uses acquire_model around the whole pipeline, rather than
    keeping this returned path without its resource lease.
    """
    from asr_models import acquire_model
    with acquire_model(support, name, owner="检查转录模型") as model:
        emit("model", "本地转录模型已就绪。")
        return str(model["paths"]["asr"])


def media_duration(path):
    result = subprocess.run([executable("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                             "-of", "json", str(path)], capture_output=True, timeout=30)
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
        if result.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return duration
    except (ValueError, KeyError):
        raise UserError("无法读取媒体时长，请重新下载。") from None


def owned_words(result, offset, lower, upper, preserve_whitespace=False, include_endpoint=False):
    """Overlapping audio windows, unique ownership by each word's midpoint."""
    segments = []
    for raw in result.get("segments", []):
        words = raw.get("words") or []
        selected = [w for w in words if lower <= offset + (w["start"] + w["end"]) / 2 < upper
                    or include_endpoint and offset + (w["start"] + w["end"]) / 2 == upper]
        if selected:
            start, end = offset + selected[0]["start"], offset + selected[-1]["end"]
            content = "".join(w["word"] for w in selected)
            if not preserve_whitespace:
                content = content.strip()
        elif not words and lower <= offset + (raw["start"] + raw["end"]) / 2 < upper:
            start, end, content = offset + raw["start"], offset + raw["end"], raw["text"].strip()
        else:
            continue
        # MLX word timing can use NumPy scalars; normalize at the JSON boundary.
        start, end = float(start), float(end)
        if content.strip() and math.isfinite(start) and math.isfinite(end):
            segment = {"start": float(max(lower, start)), "end": float(min(upper, max(start, end))), "text": content}
            if selected:
                segment["words"] = [{"start": float(max(segment["start"], w["start"] + offset)),
                                     "end": float(min(segment["end"], max(w["start"], w["end"]) + offset)),
                                     "word": w["word"]} for w in selected]
            segments.append(segment)
    return segments


def pause_boundaries(duration, silences, window=240, radius=12):
    """Prefer a real pause near each target so timing drift cannot cut a word.

    No pause means the original target is retained, with overlap still available.
    The audio intervals always partition the entire recording without gaps.
    """
    boundaries, snapped = [0.0], 0
    for target in range(window, math.ceil(duration), window):
        if duration - target < 15:
            break
        candidates = [(start + end) / 2 for start, end in silences
                      if end - start >= 0.3 and abs((start + end) / 2 - target) <= radius
                      and boundaries[-1] + 1 < (start + end) / 2 < duration - 1]
        cut = min(candidates, key=lambda v: abs(v - target)) if candidates else float(target)
        boundaries.append(round(cut, 3))
        snapped += bool(candidates)
    return boundaries + [float(duration)], snapped


def plan_windows(path, duration, folder, identity, emit):
    plan_file = folder / "transcription-plan.json"
    if plan_file.exists():
        saved = read_json(plan_file)
        cuts = saved.get("boundaries", [])
        if (saved.get("identity") == identity and len(cuts) >= 2 and cuts[0] == 0 and cuts[-1] == duration
                and all(type(v) in (int, float) and math.isfinite(v) for v in cuts)
                and all(a < b for a, b in zip(cuts, cuts[1:]))):
            return saved
    emit("transcribe", "分析语音停顿，避免在词语中间切片…")
    result = subprocess.run([executable("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
                             "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
                            capture_output=True, timeout=240)
    if result.returncode:
        raise UserError("语音切片分析失败，音轨已保留，可继续重试。")
    silences, start = [], None
    for line in result.stderr.decode("utf-8", errors="replace").splitlines():
        match = re.search(r"silence_start: ([0-9.]+)", line)
        if match:
            start = float(match[1])
        match = re.search(r"silence_end: ([0-9.]+)", line)
        if match and start is not None:
            silences.append((start, min(float(match[1]), duration)))
            start = None
    cuts, snapped = pause_boundaries(duration, silences)
    plan = {"identity": identity, "algorithm": "pause-boundaries-v2", "boundaries": cuts,
            "snappedBoundaries": snapped, "fallbackBoundaries": len(cuts) - 2 - snapped}
    write_json(plan_file, plan)
    return plan


def _file_hash(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _freeze_model(folder, name, resource, audio_hash, preserve_word_timing, legacy):
    """Freeze resources before inference; a resumed task never changes models."""
    manifest = folder / "transcription-model.json"
    version = adapter_version(resource["engine"])
    if manifest.exists():
        saved = read_json(manifest)
        if (saved.get("schemaVersion") != 1 or saved.get("modelID") != name
                or saved.get("adapterVersion") != version or saved.get("resources") != resource
                or saved.get("audioSHA256") != audio_hash
                or saved.get("preserveWordTiming") != preserve_word_timing):
            raise UserError("任务已固定转录模型、音频与引擎版本。请恢复原配置，或新建任务使用其他模型。")
        return saved
    old_identity = digest({"sha256": audio_hash, "model": name, "version": 3 if preserve_word_timing else 2})
    old_cache = folder / "checkpoints" / "whisper" / old_identity
    # Old tasks with their original windows can resume on a verified resource.
    # Newly created tasks always use the complete resource/adapter identity.
    old = legacy and name in ("turbo", "small") and old_cache.is_dir()
    identity = old_identity if old else digest({"audioSHA256": audio_hash, "resources": resource,
                                              "adapterVersion": version, "preserveWordTiming": preserve_word_timing})
    result = {"schemaVersion": 1, "modelID": name, "resources": resource, "adapterVersion": version,
              "audioSHA256": audio_hash, "preserveWordTiming": preserve_word_timing,
              "checkpointIdentity": identity, "checkpointKind": "whisper" if old else "asr"}
    write_json(manifest, result)
    return result


def _cached_segments(path, lower, upper):
    values = read_json(path)
    if not isinstance(values, list):
        raise UserError("转录检查点无效，原文件已保留。")
    if values:
        validate_segments([dict(value, id=i) for i, value in enumerate(values)])
        if any(v["start"] < lower or v["end"] > upper + 0.001 for v in values):
            raise UserError("转录检查点时间超出片段范围，原文件已保留。")
    return values


def _extract_wave(path, wave, offset, seconds):
    result = subprocess.run([executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-ss", str(offset),
                             "-i", str(path), "-t", str(seconds), "-ar", "16000", "-ac", "1",
                             "-c:a", "pcm_s16le", str(wave)], capture_output=True, timeout=120)
    if result.returncode:
        raise UserError("音频转换失败，原音频与已完成片段已保留。")


def transcribe(path, name, folder, support, emit, preserve_word_timing=False, legacy=False, asr_snapshot=None):
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise UserError("本地语音转文字需要 Apple Silicon Mac。")
    import mlx.core as mx
    if not mx.metal.is_available():
        raise UserError("Metal GPU 不可用，已停止转录，没有自动切换为纯 CPU。")
    mx.set_default_device(mx.gpu)
    if hasattr(mx, "reset_peak_memory"):
        mx.reset_peak_memory()
    folder, support, path = Path(folder), Path(support), Path(path)
    duration = media_duration(path)
    frozen = folder / "transcription-model.json"
    previous = read_json(frozen) if frozen.is_file() else None
    if previous and previous.get("modelID") != name:
        raise UserError("此任务已使用其他转录模型，不能混用检查点；请新建任务切换型号。")
    expected = previous.get("resources") if previous else asr_snapshot
    from asr_models import acquire_model
    emit("model", "检查已安装的转录模型与任务版本…")
    request_path = folder / "request.json"
    title = read_json(request_path).get("title", folder.name) if request_path.is_file() else folder.name
    with acquire_model(support, name, owner=str(title)[:200], expected=expected) as installed:
        snapshot = _freeze_model(folder, name, installed["snapshot"], _file_hash(path), preserve_word_timing, legacy)
        try:
            return _transcribe_managed(path, name, folder, emit, duration, installed, snapshot, mx)
        finally:
            release_transcription_model()


def _transcribe_managed(path, name, folder, emit, duration, installed, snapshot, mx):
    started = time.monotonic()
    engine = installed["snapshot"]["engine"]
    qwen = engine == "mlx-audio"
    if engine not in ("mlx-audio", "mlx-whisper"):
        raise UserError("当前应用不支持此转录引擎，请更新应用。")
    emit("transcribe", "使用本机 MLX Metal GPU 处理语音…")
    runtime = {"engine": engine, "device": str(mx.default_device()), "metalAvailable": True, "model": name,
               "audioSeconds": duration, "adapterVersion": snapshot["adapterVersion"],
               "modelSnapshot": installed["snapshot"], "timestampSource": "qwen3-forced-aligner" if qwen else "whisper-word-timestamps",
               "localPromptTokens": 0, "localGenerationTokens": 0,
               "computedRecognitionWindows": 0, "reusedRecognitionWindows": 0,
               "computedAlignmentWindows": 0, "reusedAlignmentWindows": 0, "recognitionTimings": []}
    runtime_file = folder / "transcription-runtime.json"
    if runtime_file.is_file():
        previous = read_json(runtime_file)
        if "elapsedSeconds" in previous:
            write_json(folder / "transcription-runtime-previous.json", previous)
    write_json(folder / "transcription-runtime.json", runtime)
    identity = snapshot["checkpointIdentity"]
    plan = plan_windows(path, duration, folder, identity, emit)
    cache = folder / "checkpoints" / snapshot["checkpointKind"] / identity
    temporary = folder / "temporary"
    temporary.mkdir(exist_ok=True, mode=0o700)
    all_segments = []
    reused, computed = 0, 0
    timings, processed = [], 0.0
    wave = temporary / "window.wav"
    adapter = QwenAdapter(installed["paths"]) if qwen else None
    runtime.update(completed=False, snappedBoundaries=plan["snappedBoundaries"], fallbackBoundaries=plan["fallbackBoundaries"])
    def save_runtime():
        runtime.update(elapsedSeconds=round(time.monotonic() - started, 2), segmentCount=len(all_segments),
                       peakMLXBytes=mx.get_peak_memory() if hasattr(mx, "get_peak_memory") else None,
                       reusedWindows=reused, computedWindows=computed, processedAudioSeconds=processed,
                       windowTimings=timings)
        write_json(runtime_file, runtime)
    windows = []
    for lower, upper in zip(plan["boundaries"], plan["boundaries"][1:]):
        offset = max(0, lower - 8)
        seconds = min(duration, upper + 8) - offset
        if qwen and seconds > 300:
            raise UserError("音频切片超过 Qwen 时间对齐范围，原音频已保留。")
        windows.append((lower, upper, offset, seconds))
    try:
        if qwen:
            # Finish recognition before loading the aligner. Each phase has its
            # own durable checkpoints, so alignment failures do not redo ASR.
            for index, (lower, upper, offset, seconds) in enumerate(windows):
                recognition_started = time.monotonic()
                checkpoint = cache / "recognition" / f"{index:04d}.json"
                emit("transcribe", f"识别文字 · {index + 1}/{len(windows)}", 0.65 * lower / duration)
                recognition_reused = checkpoint.exists()
                if recognition_reused:
                    raw = read_json(checkpoint)
                    if (not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or raw.get("audioSeconds") != seconds
                            or type(raw.get("localGenerationTokens")) is not int
                            or not 0 <= raw["localGenerationTokens"] < 8192
                            or type(raw.get("localPromptTokens")) is not int or raw["localPromptTokens"] < 0):
                        raise UserError("Qwen 识别检查点无效，原文件已保留。")
                    runtime["reusedRecognitionWindows"] += 1
                else:
                    _extract_wave(path, wave, offset, seconds)
                    raw = adapter.recognize(wave, seconds)
                    write_json(checkpoint, raw)
                    runtime["computedRecognitionWindows"] += 1
                    runtime["localPromptTokens"] += raw["localPromptTokens"]
                    runtime["localGenerationTokens"] += raw["localGenerationTokens"]
                runtime["recognitionTimings"].append({"index": index, "reused": recognition_reused,
                    "elapsedSeconds": round(time.monotonic() - recognition_started, 3)})
                save_runtime()
            adapter.unload()
        for index, (lower, upper, offset, seconds) in enumerate(windows):
            window_started = time.monotonic()
            checkpoint = cache / ("alignment" if qwen else "") / f"{index:04d}.json"
            progress = 0.65 + 0.35 * lower / duration if qwen else lower / duration
            emit("align" if qwen else "transcribe", f"{'时间对齐' if qwen else 'Metal GPU 转录'} · {index + 1}/{len(windows)}", progress)
            window_reused = checkpoint.exists()
            if window_reused:
                values = _cached_segments(checkpoint, lower, upper)
                reused += 1
                if qwen:
                    runtime["reusedAlignmentWindows"] += 1
            else:
                _extract_wave(path, wave, offset, seconds)
                if qwen:
                    raw = read_json(cache / "recognition" / f"{index:04d}.json")
                    result = {"segments": adapter.align(wave, raw["text"], seconds)}
                    runtime["computedAlignmentWindows"] += 1
                else:
                    import mlx_whisper
                    with offline_models(), contextlib.redirect_stdout(sys.stderr):
                        result = mlx_whisper.transcribe(str(wave), path_or_hf_repo=str(installed["paths"]["asr"]),
                            language="zh", task="transcribe", word_timestamps=True, verbose=None, condition_on_previous_text=False)
                values = owned_words(result, offset, lower, upper, preserve_whitespace=qwen, include_endpoint=upper == duration)
                if values:
                    validate_segments([dict(value, id=i) for i, value in enumerate(values)])
                computed += 1
                write_json(checkpoint, values)
            all_segments.extend(values)
            processed = upper
            timings.append({"index": index, "audioSeconds": upper - lower,
                            "elapsedSeconds": round(time.monotonic() - window_started, 3), "reused": window_reused,
                            "stage": "alignment" if qwen else "recognition"})
            save_runtime()
            wave.unlink(missing_ok=True)
        all_segments.sort(key=lambda s: s["start"])
        if not all_segments:
            raise UserError("没有识别到有效语音。原音频已保留，可换用其他模型新建任务。")
        values = [{**s, "id": i} for i, s in enumerate(all_segments)]
        validate_segments(values)
        emit("transcribe", "本地转录完成，正在保存原稿…", 1)
        runtime["completed"] = True
        return values
    finally:
        if adapter is not None:
            adapter.unload()
        wave.unlink(missing_ok=True)
        save_runtime()


def release_transcription_model():
    """Drop process-local ASR allocations before loading the diarization helper."""
    clear_model_cache()
