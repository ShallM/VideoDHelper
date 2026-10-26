#!/usr/bin/env python3
"""Run frozen transcripts through a selected engine; credentials stay in memory.

Run once per strategy. Results/checkpoints are private local validation artifacts;
API metering still goes to the normal application ledger. SIGTERM is resumable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

SAMPLES = {
    "short": "3261059F-A984-40D6-9E29-0F611DCF6541",
    "history": "5B82872F-2C0B-4C21-84BB-890EF21DE407",
    "technical": "D268C983-9022-410A-AE25-0D1E599BC507",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["legacy", "compact", "structured"], required=True)
    parser.add_argument("--engine", default="backend")
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", nargs="+", choices=list(SAMPLES), default=list(SAMPLES))
    parser.add_argument("--no-review", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    sys.path.insert(0, str(Path(args.engine).resolve()))
    from common import read_json, write_json, save_transcript
    from summary import summarize, export_notes
    from worker import load_config
    support = Path.home() / "Library/Application Support/VideoDHelper"
    config = load_config(support)
    config.update(summaryStrategy=args.strategy, summaryReview=not args.no_review)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    base = Path(args.output).resolve()
    for name in args.samples:
        source = support / "tasks" / SAMPLES[name]
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / "benchmark.json"
        state = read_json(manifest) if manifest.exists() else {"taskID": str(uuid.uuid4()).upper(), "attempts": []}
        if state.get("completed"):
            print(json.dumps({"sample": name, "status": "already_complete"}), flush=True)
            continue
        segments = read_json(source / "transcript.json")
        request = read_json(source / "request.json")
        save_transcript(folder, segments)
        run_config = {**config, "taskID": state["taskID"], "taskTitle": f"验证 · {args.strategy} · {name}"}
        state.update(strategy=args.strategy, review=not args.no_review, sourceTaskID=SAMPLES[name],
                     model=config["model"], transcriptSHA256=hashlib.sha256((source / "transcript.json").read_bytes()).hexdigest())
        attempt = {"startedAt": time.time(), "completed": False}
        state["attempts"].append(attempt)
        write_json(manifest, state)
        def emit(stage, message, progress=None):
            print(json.dumps({"sample": name, "stage": stage, "message": message, "progress": progress}, ensure_ascii=False), flush=True)
        try:
            notes, condensed = summarize(segments, request["title"], run_config, folder, emit)
            export_notes(folder, request["title"], request["source"], request.get("url", ""), segments, notes, condensed)
            attempt.update(completed=True, tokens=notes["tokensThisRun"])
            state["completed"] = True
        except KeyboardInterrupt:
            attempt["cancelled"] = True
            return 130
        except Exception as error:
            attempt["errorType"] = type(error).__name__
            print(json.dumps({"sample": name, "errorType": type(error).__name__, "message": str(error)[:240]}, ensure_ascii=False), flush=True)
            return 2
        finally:
            attempt["elapsedSeconds"] = round(time.time() - attempt["startedAt"], 3)
            write_json(manifest, state)
        print(json.dumps({"sample": name, "finished": attempt}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
