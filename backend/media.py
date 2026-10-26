"""yt-dlp acquisition and bounded-memory MLX Whisper transcription."""
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

from common import UserError, digest, read_json, write_json
from local_models import convert_checkpoint, existing_checkpoint, ready

MODELS = {"turbo": "mlx-community/whisper-large-v3-turbo", "small": "mlx-community/whisper-small-mlx"}


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
    if name not in MODELS:
        raise UserError("请选择支持的本地 Whisper 模型。")
    directory = support / "models" / name
    if ready(directory):
        emit("model", "复用已准备好的本地 MLX 模型。")
        return str(directory)
    checkpoint = existing_checkpoint(name)
    if checkpoint:
        return convert_checkpoint(name, checkpoint, directory, emit)
    from huggingface_hub import snapshot_download
    # snapshot_download verifies its local metadata and resumes incomplete downloads.
    emit("model", "准备本地 Whisper 模型；首次下载约 1.6 GB…" if name == "turbo" else "准备本地 Whisper 模型；首次下载约 0.5 GB…")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            path = snapshot_download(MODELS[name], local_dir=str(directory),
                                     allow_patterns=["config.json", "weights.npz", "*.safetensors"],
                                     max_workers=2)
        return path
    except Exception:
        raise UserError("Whisper 模型下载未完成。请检查 Hugging Face 连接，继续处理会复用已下载的文件。") from None


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


def owned_words(result, offset, lower, upper):
    """Overlapping audio windows, unique ownership by each word's midpoint."""
    segments = []
    for raw in result.get("segments", []):
        words = raw.get("words") or []
        selected = [w for w in words if lower <= offset + (w["start"] + w["end"]) / 2 < upper]
        if selected:
            start, end = offset + selected[0]["start"], offset + selected[-1]["end"]
            content = "".join(w["word"] for w in selected).strip()
        elif not words and lower <= offset + (raw["start"] + raw["end"]) / 2 < upper:
            start, end, content = offset + raw["start"], offset + raw["end"], raw["text"].strip()
        else:
            continue
        # MLX word timing can use NumPy scalars; normalize at the JSON boundary.
        start, end = float(start), float(end)
        if content and math.isfinite(start) and math.isfinite(end):
            segments.append({"start": float(max(lower, start)), "end": float(min(upper, max(start, end))), "text": content})
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


def transcribe(path, name, folder, support, emit):
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise UserError("本次本地 Whisper 引擎需要 Apple Silicon Mac；Intel Mac 可使用平台或导入字幕。")
    import mlx.core as mx
    if not mx.metal.is_available():
        raise UserError("Metal GPU 不可用，已停止转录，没有自动切换为纯 CPU。")
    mx.set_default_device(mx.gpu)
    duration = media_duration(path)
    model_path = prepare_model(name, support, emit)
    started = time.monotonic()
    emit("transcribe", "载入 Whisper 模型；使用本机 MLX Metal GPU 加速…")
    runtime = {"engine": "mlx-whisper", "device": str(mx.default_device()), "metalAvailable": True, "model": name,
               "audioSeconds": duration}
    runtime_file = folder / "transcription-runtime.json"
    if runtime_file.is_file():
        previous = read_json(runtime_file)
        if "elapsedSeconds" in previous:
            write_json(folder / "transcription-runtime-previous.json", previous)
    write_json(folder / "transcription-runtime.json", runtime)
    import mlx_whisper
    # Key checkpoints to the actual audio contents, model and boundary algorithm.
    file_hash = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            file_hash.update(block)
    identity = digest({"sha256": file_hash.hexdigest(), "model": name, "version": 2})
    plan = plan_windows(path, duration, folder, identity, emit)
    cache = folder / "checkpoints" / "whisper" / identity
    temporary = folder / "temporary"
    temporary.mkdir(exist_ok=True, mode=0o700)
    all_segments = []
    reused, computed = 0, 0
    timings = []
    runtime.update(completed=False, snappedBoundaries=plan["snappedBoundaries"], fallbackBoundaries=plan["fallbackBoundaries"])
    def save_runtime(upper):
        runtime.update(elapsedSeconds=round(time.monotonic() - started, 2), segmentCount=len(all_segments),
                       peakMLXBytes=mx.get_peak_memory() if hasattr(mx, "get_peak_memory") else None,
                       reusedWindows=reused, computedWindows=computed, processedAudioSeconds=upper,
                       windowTimings=timings)
        write_json(runtime_file, runtime)
    for index, (lower, upper) in enumerate(zip(plan["boundaries"], plan["boundaries"][1:])):
        window_started = time.monotonic()
        checkpoint = cache / f"{index:04d}.json"
        emit("transcribe", f"Metal GPU 转录 · {int(lower // 60)}:{int(lower % 60):02d} / {int(duration // 60)}:{int(duration % 60):02d}", lower / duration)
        if checkpoint.exists():
            all_segments.extend(read_json(checkpoint))
            reused += 1
            timings.append({"index": index, "audioSeconds": upper - lower, "elapsedSeconds": 0, "reused": True})
            save_runtime(upper)
            continue
        offset = max(0, lower - 8)
        wave = temporary / "window.wav"
        result = subprocess.run([executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-ss", str(offset),
                                 "-i", str(path), "-t", str(min(duration, upper + 8) - offset),
                                 "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wave)],
                                capture_output=True, timeout=120)
        if result.returncode:
            raise UserError("音频转换失败，下载结果已保留。")
        with contextlib.redirect_stdout(sys.stderr):
            result = mlx_whisper.transcribe(str(wave), path_or_hf_repo=model_path, language="zh", task="transcribe",
                                            word_timestamps=True, verbose=None, condition_on_previous_text=False)
        values = owned_words(result, offset, lower, upper)
        computed += 1
        write_json(checkpoint, values)
        all_segments.extend(values)
        timings.append({"index": index, "audioSeconds": upper - lower,
                        "elapsedSeconds": round(time.monotonic() - window_started, 3), "reused": False})
        save_runtime(upper)
        wave.unlink(missing_ok=True)
    all_segments.sort(key=lambda s: s["start"])
    if not all_segments:
        raise UserError("Whisper 没有识别到有效语音。媒体已保留，可改用其他模型或字幕。")
    emit("transcribe", "本地转录完成，正在保存原稿…", 1)
    runtime["completed"] = True
    save_runtime(duration)
    return [{"id": i, **s} for i, s in enumerate(all_segments)]
