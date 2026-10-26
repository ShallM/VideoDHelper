#!/usr/bin/env python3
"""Real worker acceptance run with controlled transcription/summary interruptions.

Uses an already extracted yt-dlp info JSON. Credentials stay in the normal local
settings, never in this report. Run with the project's virtual environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from common import read_json, write_json


def fingerprints(paths):
    return {str(p): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "mtime": p.stat().st_mtime_ns} for p in paths}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--info", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--job", type=Path, help="Reuse an existing task after a fix; completed transcripts are reused.")
    parser.add_argument("--interruptions", choices=["both", "summary", "none"], default="both")
    parser.add_argument("--crash-transcription", action="store_true", help="Use SIGKILL to simulate an ungraceful exit.")
    args = parser.parse_args()
    os.umask(0o077)
    info = read_json(args.info)
    support = Path.home() / "Library/Application Support/VideoDHelper"
    identifier = args.job.name if args.job else str(uuid.uuid4()).upper()
    folder = args.job or support / "tasks" / identifier
    video_id = info["id"].split("_p")[0]
    url = "https://www.bilibili.com/video/" + video_id + "/"
    now = time.time()
    source = "本地 Whisper · Large v3 Turbo · 中文"
    if not args.job:
        write_json(folder / "job.json", dict(id=identifier, title=info["title"], url=url, source=source,
                   createdAt=now, updatedAt=now, state="pending", stage="prepare", message="长视频验证", mediaFiles=[]))
        write_json(folder / "request.json", dict(title=info["title"], url=url, source=source, segments=[],
                   useWhisper=True, mediaMode="needed", keepMedia=True, videoFormatID="", audioFormatID="",
                   videoIncludesAudio=False, container="mp4", whisperModel="turbo"))
    report = {"jobID": identifier, "directory": str(folder), "url": url, "duration": info["duration"], "runs": []}
    write_json(args.report, report)
    phases = {"both": ("transcribe", "summary", None), "summary": ("summary", None), "none": (None,)}
    for interruption in phases[args.interruptions]:
        index = len(report["runs"])
        run = {"interruption": interruption, "startedAt": time.time()}
        output = args.report.parent / f"worker-{index}.log"
        observed, snapshot, sent_at = None, {}, None
        with output.open("wb") as log:
            child = subprocess.Popen([sys.executable, "-u", str(ROOT / "backend/worker.py"), "--job", str(folder),
                                      "--support", str(support), "--env-file", str(ROOT / ".env")], stdout=log, stderr=log)
            try:
                while child.poll() is None:
                    record = read_json(folder / "job.json")
                    state = (record["stage"], record["message"])
                    if state != observed:
                        print(json.dumps({"run": index + 1, "stage": state[0], "message": state[1]}, ensure_ascii=False), flush=True)
                        observed = state
                        run.setdefault("firstStageTimes", {}).setdefault(state[0], time.time())
                    if interruption and sent_at is None and record["state"] == "running" and record["updatedAt"] >= run["startedAt"]:
                        if interruption == "transcribe":
                            plan_file = folder / "transcription-plan.json"
                            paths = sorted((folder / "checkpoints/whisper" / read_json(plan_file)["identity"]).glob("*.json")) if plan_file.exists() else []
                        else:
                            paths = sorted((folder / "checkpoints/summary").glob("*.json"))
                        if record["stage"] == interruption and len(paths) >= 2:
                            snapshot = fingerprints(paths)
                            run["checkpointCountAtInterrupt"] = len(snapshot)
                            run["interruptAt"] = sent_at = time.time()
                            if interruption == "transcribe" and args.crash_transcription:
                                os.killpg(child.pid, signal.SIGKILL)
                                run["forcedKill"] = True
                            else:
                                child.send_signal(signal.SIGTERM)
                    if sent_at and child.poll() is None and time.time() - sent_at > 5:
                        os.killpg(child.pid, signal.SIGKILL)
                        run["forcedKill"] = True
                    time.sleep(0.2)
                run["exitCode"] = child.wait()
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
        run["endedAt"] = time.time()
        run["checkpointFingerprints"] = snapshot
        run["stateAfter"] = read_json(folder / "job.json")["state"]
        if (folder / "transcription-runtime.json").exists():
            run["transcriptionRuntime"] = read_json(folder / "transcription-runtime.json")
        for prior in report["runs"]:
            saved = prior["checkpointFingerprints"]
            prior["checkpointsUnchangedAfterResume"] = fingerprints([Path(p) for p in saved]) == saved
        report["runs"].append(run)
        write_json(args.report, report)
        if interruption and not sent_at:
            raise SystemExit("Worker stopped before the intended interruption; inspect the saved job.")
        if not interruption and run["exitCode"] != 0:
            raise SystemExit("Final run did not complete; results remain resumable.")
    transcript = read_json(folder / "transcript.json")
    condensed = read_json(folder / "condensed.json")
    notes = read_json(folder / "notes.json")
    report["result"] = {"segments": len(transcript), "condensed": len(condensed), "chapters": len(notes["chapters"]),
                         "exactCondensedCoverage": [i for c in condensed for i in c["source_ids"]] == [s["id"] for s in transcript],
                         "transcriptEndSeconds": transcript[-1]["end"]}
    write_json(args.report, report)
    print(json.dumps({"jobID": identifier, "result": report["result"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
