#!/usr/bin/env python3
"""One isolated task process, JSON progress protocol consumed by the native app."""
import argparse
import fcntl
import contextlib
import os
from pathlib import Path
import signal
import sys
import time
import threading
import urllib.parse

from common import UserError, clean_media, read_json, save_transcript, validate_segments, write_json
from media import download, transcribe
from summary import OutputLimit, export_notes, summarize


def load_config(support, env_file=None):
    result = {"baseURL": "https://api.icompify.com/v1", "model": "deepseek-v4.1-flash", "apiKey": "", "reasoningEffort": "low"}
    if env_file and Path(env_file).is_file():
        for line in Path(env_file).read_text().splitlines():
            key, sep, value = line.partition("=")
            field = {"VIDEOHELPER_BASE_URL": "baseURL", "VIDEOHELPER_MODEL": "model", "VIDEOHELPER_API_KEY": "apiKey"}.get(key.strip())
            if sep and field:
                result[field] = value.strip().strip('"\'')
    path = support / "settings.json"
    if path.exists():
        result.update(read_json(path))
    preferences = support / "preferences.json"
    if preferences.exists():
        saved = read_json(preferences)
        if saved.get("schemaVersion") != 1:
            raise UserError("设置文件版本无法读取，已保留原文件，请检查设置。")
        provider = next((p for p in saved.get("providers", []) if p.get("id") == saved.get("defaultProviderID")), None)
        model = next((m for m in (provider or {}).get("models", []) if m.get("id") == saved.get("defaultModelID")), None)
        if not provider or not provider.get("enabled") or not model:
            raise UserError("默认模型不可用，请在设置中选择已启用的服务商和模型。")
        result = {"baseURL": provider["baseURL"], "apiKey": provider.get("apiKey", ""), "model": model["modelID"],
                  "reasoningEffort": model.get("reasoningEffort", "default"), "providerID": provider["id"],
                  "providerName": provider["name"], "modelConfigID": model["id"], "modelName": model["name"],
                  "pricing": model.get("pricing"), "concurrency": saved.get("general", {}).get("summaryConcurrency", 2)}
    result["usageRoot"] = str(support)
    return result


def validate_request(request):
    kind = request.get("sourceKind", "bilibili")
    if kind not in ("bilibili", "recording"):
        raise UserError("任务来源类型无效。")
    if kind == "recording":
        if request.get("recordingAction", "transcribe") not in ("transcribe", "summarize") or request.get("diarizationMode", "off") not in ("two", "off"):
            raise UserError("录音处理选项无效。")
        if not isinstance(request.get("title"), str) or not request["title"].strip():
            raise UserError("录音标题无效。")
        return
    if request.get("mediaMode") not in ("needed", "audio", "video") or type(request.get("keepMedia")) is not bool:
        raise UserError("任务的媒体选项无效。")
    if request.get("summaryStrategy", "legacy") not in ("legacy", "structured", "compact"):
        raise UserError("任务的总结方案无效。")
    url = urllib.parse.urlsplit(request.get("url", ""))
    if request.get("useWhisper") or request["mediaMode"] != "needed":
        if url.scheme != "https" or url.hostname not in ("www.bilibili.com", "bilibili.com", "b23.tv") or url.username or url.password or url.port:
            raise UserError("该任务缺少有效的 B 站视频链接。")


def run(folder, support, env_file=None):
    folder, support = Path(folder).resolve(), Path(support).resolve()
    request = read_json(folder / "request.json")
    validate_request(request)
    record = read_json(folder / "job.json")
    if record["id"] != folder.name:
        raise UserError("任务目录与记录不匹配。")
    if record.get("sourceKind", "bilibili") != request.get("sourceKind", "bilibili"):
        raise UserError("任务来源与记录不匹配。")

    def emit(stage, message, progress=None):
        record.update(stage=stage, message=message, progress=progress, updatedAt=time.time(), state="running")
        write_json(folder / "job.json", record)
        print(__import__("json").dumps({"stage": stage, "message": message, "progress": progress}, ensure_ascii=False), flush=True)

    try:
        emit("prepare", "准备任务，检查已完成的阶段…")
        if request.get("sourceKind") == "recording":
            # Local ASR and diarization must not require valid cloud settings.
            from recording import run_recording
            return run_recording(request, record, folder, support, emit, load_config, env_file)
        # Snapshot settings at the beginning. Editing settings affects the next run, not a running transcription.
        config = load_config(support, env_file)
        config.update(taskID=record["id"], taskTitle=request["title"])
        # Old requests remain on the old engine. Changing the default never
        # silently switches an interrupted task; comparisons are separate jobs.
        config["summaryStrategy"] = request.get("summaryStrategy") or "legacy"
        config["summaryReview"] = request.get("summaryReview", True)
        transcript = folder / "transcript.json"
        # If text is durable, retries need not download previously cleaned media.
        needs_media = (not transcript.exists() and request["useWhisper"]) or request["mediaMode"] != "needed"
        media = download(request, folder, support, emit) if needs_media else None
        if transcript.exists():
            segments = read_json(transcript)
            validate_segments(segments)
            emit("transcript", "复用已保存的原稿。")
        elif request["useWhisper"]:
            if media is None:
                raise UserError("没有取得用于转录的音轨。")
            segments = transcribe(media, request.get("asrModelID") or request.get("whisperModel", "turbo"),
                                  folder, support, emit, legacy=not bool(request.get("asrModelID")))
            save_transcript(folder, segments)
        else:
            segments = request["segments"]
            save_transcript(folder, segments)
            emit("transcript", f"已保存 {len(segments)} 段来源字幕。")
        notes, condensed = summarize(segments, request["title"], config, folder, emit)
        export_notes(folder, request["title"], request["source"], request.get("url", ""), segments, notes, condensed)
        # Only clean after ALL three text products have been atomically committed.
        warning = ""
        if not request["keepMedia"]:
            emit("cleanup", "文字已保存，正在清理本任务的媒体…")
            try:
                clean_media(folder)
                (folder / "media-manifest.json").unlink(missing_ok=True)
            except OSError:
                warning = "文字已完成，但媒体清理失败；可用“清理本任务媒体”重试。"
        else:
            temporary = folder / "temporary" / "window.wav"
            temporary.unlink(missing_ok=True)
        media_files = [str(p.relative_to(folder)) for p in (folder / "media").glob("source.*") if p.is_file() and p.suffix not in (".part", ".ytdl")]
        record.update(state="completed", stage="completed", progress=1.0, updatedAt=time.time(),
                      message=warning or f"已完成：{len(segments)} 段原稿、精简台词与学习笔记。", mediaFiles=media_files,
                      segmentCount=len(segments))
        write_json(folder / "job.json", record)
        print('{"stage":"completed","message":"处理完成","progress":1}', flush=True)
        return 0
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            state, message = "cancelled", "已取消。已完成的阶段和媒体保留，可继续处理或手动清理。"
        elif isinstance(error, UserError):
            state, message = "failed", str(error)
        elif isinstance(error, (ValueError, KeyError, TypeError, OutputLimit)):
            state, message = "failed", "模型输出或任务数据未通过检查。原稿和已完成的分段已保留，可继续重试。"
        else:
            state, message = "failed", f"{record.get('stage', '当前')}阶段失败（{type(error).__name__}）。已保留完成的数据。"
        record.update(state=state, message=message, updatedAt=time.time(), progress=None)
        write_json(folder / "job.json", record)
        print(__import__("json").dumps({"stage": state, "message": message}), flush=True)
        return 2


def main():
    os.umask(0o077)
    # The app cancels this process group, including ffmpeg and downloader children.
    try:
        os.setpgrp()
    except PermissionError:
        pass
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return
    threading.Thread(target=watch_parent, daemon=True).start()
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--job")
    operation.add_argument("--models", choices=("list", "download", "remove", "clear"))
    parser.add_argument("--model-id")
    parser.add_argument("--support", required=True)
    parser.add_argument("--env-file")
    args = parser.parse_args()
    if args.models:
        from asr_models import run_cli
        return run_cli(args.models, args.model_id, Path(args.support))
    with (Path(args.job) / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"stage":"failed","message":"该任务已在另一个进程中运行。"}', flush=True)
            return 3
        return run(args.job, args.support, args.env_file)


if __name__ == "__main__":
    sys.exit(main())
