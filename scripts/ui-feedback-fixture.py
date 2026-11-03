#!/usr/bin/env python3
"""Isolated UI fixtures: local HTTP only, synthetic text, no ASR or paid model calls.

Run this server, then launch a test app with VIDEOHELPER_DATA_DIRECTORY set to
the printed support directory. Never point this at an existing personal library.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from meeting_contract import DRAFT_PROMPT, EVIDENCE_PROMPT, REVIEW_PROMPT


class LocalModel(BaseHTTPRequestHandler):
    delay = 12
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.loads(body["messages"][-1]["content"])
        prompt = body["messages"][0]["content"]
        time.sleep(self.delay)
        if "失败演示" in payload.get("title", ""):
            self.send_response(401); self.end_headers()
            self.wfile.write(b'{"error":{"message":"Local fixture: authentication failed"}}')
            return
        proof = lambda part: [{"first_part": part, "last_part": part}]
        if prompt == DRAFT_PROMPT:
            value = {"overview": {"text": "准备原稿后检查界面反馈，再决定是否发布。", "u": [0]},
                     "chapters": [{"title": "界面验证安排", "claims": [
                         {"text": "原稿已准备好。", "evidence": proof(0)},
                         {"text": "先检查界面反馈，再决定是否发布。", "evidence": proof(1)}]}],
                     "action_items": [], "uncertainties": []}
            phase = "draft"
        elif prompt == EVIDENCE_PROMPT:
            value = {"items": [{"text": "原稿已准备好。", "evidence": proof(0)},
                               {"text": "先检查界面反馈，再决定是否发布。", "evidence": proof(1)}]}
            phase = "evidence"
        elif prompt == REVIEW_PROMPT:
            value = {"patches": [], "additions": [], "issues": []}
            phase = "review"
        elif "segments" in payload:
            rows = payload["segments"]
            value = {"condensed": [{"source_ids": [r["id"]], "text": r["text"]} for r in rows],
                     "notes": [{"source_ids": [r["id"] for r in rows], "title": "界面验收", "detail": "检查进度反馈。"}]}
            phase = "video_chunk"
        else:
            value = {"overview": "本地界面验收。", "key_points": [{"note_ids": [0], "text": "检查进度反馈。"}], "uncertainties": []}
            phase = "video_overview"
        print(json.dumps({"phase": phase, "at": time.time()}), flush=True)
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        try:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except BrokenPipeError: pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("support", type=Path)
    parser.add_argument("--delay", type=float, default=12)
    args = parser.parse_args()
    root = args.support.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()): raise SystemExit("Use an empty fixture directory; personal data is never overwritten.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    LocalModel.delay = args.delay
    write = lambda path, value: path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    rows = [{"id": i, "start": i * 3., "end": (i + 1) * 3., "speaker": "A" if i != 1 else "B", "text": text}
            for i, text in enumerate(["原稿已准备好。", "先检查界面反馈，再决定是否发布。", "测试只访问本机模拟服务。"])]
    for workspace, title in [("recording", "界面验收 · 会议纪要"), ("recording", "界面验收 · 失败演示"), ("bilibili", "界面验收 · 视频字幕")]:
        folder = root / ("recording-tasks" if workspace == "recording" else "tasks") / str(uuid.uuid4()).upper()
        folder.mkdir(parents=True)
        request = {"title": title, "url": "", "source": "本地合成样例", "segments": rows,
                   "useWhisper": False, "mediaMode": "needed", "keepMedia": False, "videoFormatID": "", "audioFormatID": "",
                   "videoIncludesAudio": False, "container": "mp4", "whisperModel": "turbo", "summaryStrategy": "legacy", "sourceKind": workspace}
        if workspace == "recording": request.update(dialogueStrategy="meeting-v3", recordingAction="summarize", diarizationMode="off", localAudioFile="source/original.m4a")
        job = {"id": folder.name, "title": title, "url": "", "source": request["source"], "createdAt": time.time(), "updatedAt": time.time(),
               "state": "transcribed" if workspace == "recording" else "pending", "stage": "transcribed", "message": "仅供界面验收，不消耗真实模型额度。",
               "mediaFiles": [], "segmentCount": len(rows), "summaryStrategy": "legacy", "sourceKind": workspace}
        if workspace == "recording": job["dialogueStrategy"] = "meeting-v3"
        for name, value in [("request.json", request), ("job.json", job), ("transcript.json", rows)]: write(folder / name, value)
    write(root / "settings.json", {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "model": "local-ui-test", "apiKey": "local-test-only", "reasoningEffort": "high"})
    write(root / "selected-workspace.json", "recording")
    print(json.dumps({"support": str(root), "port": server.server_port}), flush=True)
    server.serve_forever()


if __name__ == "__main__": main()
