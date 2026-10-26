#!/usr/bin/env python3
"""Validate a relocated app with no Homebrew/Python PATH and no paid API calls."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]


def command(*args, **kwargs):
    return subprocess.check_output([str(a) for a in args], text=True, **kwargs).strip()


def macho(path):
    if path.is_symlink() or not path.is_file(): return False
    with path.open("rb") as file:
        return file.read(4) in [b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"]


class MockModel(BaseHTTPRequestHandler):
    requests = 0
    delay = 0
    def log_message(self, *_): pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests += 1
        if self.delay: time.sleep(self.delay)
        data = json.loads(payload["messages"][-1]["content"])
        if "segments" in data:
            rows = data["segments"]
            value = {"condensed": [{"source_ids": [r["id"]], "text": r["text"]} for r in rows],
                     "notes": [{"source_ids": [r["id"] for r in rows], "title": "安装验收", "detail": "这是一份仅用于验证打包和恢复的本地测试稿。"}]}
        else:
            value = {"overview": "本地模拟接口完成整理。", "key_points": [{"note_ids": [0], "text": "原稿与检查点已保存。"}], "uncertainties": []}
        body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "prompt_tokens_details": {"cached_tokens": 0}}}).encode()
        try:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        except BrokenPipeError: pass


def fixture(root, port):
    job_id = str(uuid.uuid4()).upper()
    folder = root / "tasks" / job_id
    folder.mkdir(parents=True)
    rows = [{"id": i, "start": float(i * 2), "end": float(i * 2 + 2), "text": text} for i, text in enumerate([
        "这是 VideoDHelper 的安装验收。", "验证独立运行时、字幕整理和中断恢复。", "测试仅访问本机模拟接口，不消耗真实模型额度。"])]
    request = {"title": "安装验收 · 本地字幕", "url": "", "source": "本地测试字幕", "segments": rows,
               "useWhisper": False, "mediaMode": "needed", "keepMedia": False, "videoFormatID": "", "audioFormatID": "",
               "videoIncludesAudio": False, "container": "mp4", "whisperModel": "turbo", "summaryStrategy": "legacy"}
    job = {"id": job_id, "title": request["title"], "url": "", "source": request["source"], "createdAt": time.time(),
           "updatedAt": time.time(), "state": "pending", "stage": "prepare", "message": "用于打包验收的本地任务", "progress": None,
           "mediaFiles": [], "segmentCount": len(rows), "summaryStrategy": "legacy"}
    for name, value in [("request.json", request), ("job.json", job)]:
        (folder / name).write_text(json.dumps(value, ensure_ascii=False))
    (root / "settings.json").write_text(json.dumps({"baseURL": f"http://127.0.0.1:{port}/v1", "model": "local-installation-test", "apiKey": "local-test-only", "reasoningEffort": "low"}))
    (root / "sample.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n这是独立安装包的字幕导入测试。\n")
    return folder


def inspect_app(app):
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    minimum = tuple(map(int, info["LSMinimumSystemVersion"].split(".")))
    count = 0
    for path in app.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(app):
            raise ValueError("Escaping symlink: " + str(path.relative_to(app)))
        if not macho(path): continue
        count += 1
        if command("lipo", "-archs", path) != "arm64":
            raise ValueError("Unexpected architecture: " + str(path.relative_to(app)))
        headers = command("otool", "-l", path)
        for block in headers.split("Load command "):
            if "LC_BUILD_VERSION" in block:
                match = re.search(r"\bminos\s+(\d+(?:\.\d+)+)", block)
            elif "LC_VERSION_MIN_MACOSX" in block:
                match = re.search(r"\bversion\s+(\d+(?:\.\d+)+)", block)
            else: continue
            if match:
                actual = tuple(map(int, match[1].split(".")))
                if (actual + (0, 0))[:3] > (minimum + (0, 0))[:3]:
                    raise ValueError("Deployment target mismatch: " + str(path.relative_to(app)) + " " + match[1])
        own_ids = command("otool", "-D", path).splitlines()[1:]
        for row in command("otool", "-L", path).splitlines()[1:]:
            dependency = row.strip().split(" (", 1)[0]
            if dependency not in own_ids and not dependency.startswith(("@", "/usr/lib/", "/System/Library/")):
                raise ValueError("External developer dependency: " + str(path.relative_to(app)))
    if (app / "Contents/Resources/development-env-path.txt").exists(): raise ValueError("Development config pointer found")
    command("codesign", "--verify", "--deep", "--strict", app)
    return {"version": info["CFBundleShortVersionString"], "machoFiles": count, "architecture": "arm64", "maximumDeploymentTarget": info["LSMinimumSystemVersion"], "externalDeveloperLibraries": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ui-fixture", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockModel)
    if args.ui_fixture:
        root = args.ui_fixture.resolve(); root.mkdir(parents=True, exist_ok=True)
        folder = fixture(root, server.server_port)
        MockModel.delay = 20
        print(json.dumps({"support": str(root), "job": folder.name, "pid": os.getpid()}), flush=True)
        server.serve_forever()
        return
    app = args.app.resolve()
    report = inspect_app(app)
    env = {**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": "/nonexistent/should-be-ignored"}
    resources = app / "Contents/Resources"
    report["runtime"] = json.loads(command(resources / "runtime-check", env=env, cwd="/"))
    report["downloader"] = command(resources / "yt-dlp", "--version", env=env, cwd="/")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="VideoDHelper-installation-") as temporary:
            support = Path(temporary)
            folder = fixture(support, server.server_port)
            command(resources / "video-worker", "--job", folder, "--support", support, env=env, cwd="/")
            assert json.loads((folder / "job.json").read_text())["state"] == "completed"
            checkpoints = {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*.json") if "checkpoints" in str(p)}
            first_requests = MockModel.requests
            command(resources / "video-worker", "--job", folder, "--support", support, env=env, cwd="/")
            assert MockModel.requests == first_requests
            assert all(hashlib.sha256((folder / p).read_bytes()).hexdigest() == sha for p, sha in checkpoints.items())
            report["worker"] = {"completed": True, "mockRequests": first_requests, "resumeNewRequests": 0, "unchangedCheckpoints": len(checkpoints),
                                "exports": all((folder / p).exists() for p in ["transcript.txt", "source.srt", "condensed.md", "study-notes.md"])}
            audio = support / "input.wav"
            with wave.open(str(audio), "wb") as file:
                file.setnchannels(1); file.setsampwidth(2); file.setframerate(44100); file.writeframes(bytes(44100 * 2))
            command(resources / "bin/ffmpeg", "-nostdin", "-v", "error", "-i", audio, "-ac", "1", "-ar", "16000", support / "output.wav", env=env)
            duration = command(resources / "bin/ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", support / "output.wav", env=env)
            report["media"] = {"resampleSeconds": float(duration), "ffmpegLicense": "LGPL-2.1-or-later"}
    finally:
        server.shutdown(); server.server_close()
    command("codesign", "--verify", "--deep", "--strict", app)
    report["signatureUnchangedAfterUse"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
