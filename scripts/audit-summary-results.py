#!/usr/bin/env python3
"""Audit frozen A/B artifacts without another model request.

Structural coverage is deliberately separate from the human-readable semantic
rubric. This script must never label a keyword hit as semantic correctness.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from common import read_json, write_json
from usage import normalize_usage


def public_metrics(value):
    """Keep measurements reproducible without publishing local ledger identifiers."""
    if isinstance(value, dict):
        return {k: public_metrics(v) for k, v in value.items()
                if k not in {"taskID", "sourceTaskID", "eventIDs", "source_task"}}
    if isinstance(value, list):
        return [public_metrics(item) for item in value]
    return value


def billing(events):
    totals, unknown = {}, 0
    for event in events:
        usage, price = event.get("usage", {}), event.get("pricing", {})
        required = ("inputTokens", "outputTokens", "cachedInputTokens")
        rates = ("inputPerMillion", "outputPerMillion", "cachedInputPerMillion")
        if usage.get("invalid") or any(k not in usage for k in required) or any(k not in price for k in rates):
            unknown += 1
            continue
        cost = ((usage["inputTokens"] - usage["cachedInputTokens"]) * price["inputPerMillion"]
                + usage["cachedInputTokens"] * price["cachedInputPerMillion"]
                + usage["outputTokens"] * price["outputPerMillion"]) / 1_000_000
        currency = price["currency"]
        totals[currency] = totals.get(currency, 0) + cost
    return {"pricedSubtotal": {k: round(v, 6) for k, v in totals.items()},
            "unknownCostRequests": unknown,
            "meaning": "Uses request-time prices; subtotal excludes unknown requests and is not a provider invoice."}


def inspect(folder):
    state = read_json(folder / "benchmark.json")
    if not state.get("completed"):
        raise ValueError(f"Benchmark is incomplete: {folder}")
    source = read_json(folder / "transcript.json")
    notes = read_json(folder / "notes.json")
    condensed = read_json(folder / "condensed.json")
    allowed = set(range(len(source)))
    citations = notes["key_points"] + notes["uncertainties"] + notes["chapters"] + condensed
    citations += [c for chapter in notes["chapters"] for c in chapter.get("claims", [])]
    legal = all(item["source_ids"] and item["source_ids"] == sorted(set(item["source_ids"]))
                and set(item["source_ids"]) <= allowed for item in citations)
    covered = {i for item in condensed for i in item["source_ids"]}
    totals = {k: 0 for k in ("inputTokens", "outputTokens", "reasoningTokens", "reportedTotalTokens", "cachedInputTokens")}
    records = []
    for path in folder.glob("checkpoints/*/usage.jsonl"):
        records += [json.loads(line) for line in path.read_text().splitlines()]
    cache_reported = 0
    cache_input = 0
    for record in records:
        usage = normalize_usage(record.get("usage"))
        if usage["invalid"]:
            raise ValueError("Invalid metering record")
        for field in totals:
            totals[field] += usage.get(field, 0)
        if "cachedInputTokens" in usage:
            cache_reported += 1
            cache_input += usage.get("inputTokens", 0)
    result = {"strategy": state["strategy"], "model": state["model"], "taskID": state["taskID"],
        "sourceTaskID": state["sourceTaskID"], "sourceSHA256": state["transcriptSHA256"],
        "sourceSegments": len(source), "sourceCharacters": sum(len(s["text"]) for s in source),
        "chapters": len(notes["chapters"]), "claims": sum(len(c.get("claims", [c])) for c in notes["chapters"]),
        "uncertainties": len(notes["uncertainties"]), "responseCount": len(records),
        "elapsedSeconds": round(sum(a["elapsedSeconds"] for a in state["attempts"]), 3),
        "attemptCount": len(state["attempts"]), "usage": totals,
        "cacheReporting": {"reportedResponses": cache_reported, "allResponses": len(records),
                           "inputTokensOfReportedResponses": cache_input,
                           "meaning": "Missing cache fields are unknown, not zero."},
        "eventIDs": [r["eventID"] for r in records if r.get("eventID")],
        "legalCitations": legal, "condensedCoversAllSourceIDs": covered == allowed,
        "note": "These are structural checks, not semantic zero-omission claims."}
    if (folder / "reading-source.json").exists():
        rows = read_json(folder / "reading-source.json")["paragraphs"]
        review = read_json(folder / "review-report.json")
        result.update(sourceParagraphs=len(rows), rereadCount=review["rereadCount"],
            losslessCompactText="".join(r["text"] for r in rows) == "".join(s["text"] for s in source),
            reviewReadAllParagraphs=review["reviewedParagraphs"] == list(range(len(rows))))
    return result


def recovery(folder):
    result = []
    for path in folder.glob("interruption*evidence.json"):
        evidence = read_json(path)
        files = evidence.get("files", evidence.get("checkpoints", {}))
        if not files:
            raise ValueError("Interruption evidence has no checkpoints")
        unchanged = all((folder / "checkpoints/summary-v2" / name).exists()
            and hashlib.sha256((folder / "checkpoints/summary-v2" / name).read_bytes()).hexdigest() == saved["sha256"]
            and (folder / "checkpoints/summary-v2" / name).stat().st_mtime_ns == saved["mtime_ns"]
            for name, saved in files.items())
        result.append({"phase": evidence["phase"], "checkpointCount": len(files),
                       "exitSeconds": evidence.get("exitSeconds"), "allBytesAndMtimesUnchanged": unchanged})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="local-data/validation/summary-v2")
    parser.add_argument("--output", default="docs/research-data/summary-validation-0.5.json")
    args = parser.parse_args()
    base = Path(args.base)
    result = {"samples": {}, "recovery": {}}
    for name in ("short", "history", "technical"):
        old, new = inspect(base / "legacy" / name), inspect(base / "final-cold" / name)
        if old["sourceSHA256"] != new["sourceSHA256"] or old["model"] != new["model"]:
            raise ValueError("A/B source or model mismatch")
        result["samples"][name] = {"legacy": old, "structured": new}
        if name != "short":
            result["recovery"][name] = recovery(base / "structured" / name)
    ledger = Path.home() / "Library/Application Support/VideoDHelper/usage/events"
    events = {p.stem: read_json(p) for p in ledger.glob("*.json")}
    benchmark_events = set()
    for pair in result["samples"].values():
        for sample in pair.values():
            ids = sample["eventIDs"]
            sample["billing"] = billing([events[i] for i in ids if i in events])
            benchmark_events.update(ids)
    task_ids = {read_json(p)["taskID"] for p in base.rglob("benchmark.json")}
    development = [e for i, e in events.items() if e.get("taskID") in task_ids and i not in benchmark_events]
    result["development"] = {"httpAttempts": len(development),
        "reportedTotalTokens": sum(e.get("usage", {}).get("reportedTotalTokens", 0) for e in development),
        "unknownUsageAttempts": sum("reportedTotalTokens" not in e.get("usage", {}) for e in development),
        "billing": billing(development),
        "note": "All benchmark task IDs, deduplicated across saved variants; excludes final A/B response IDs. Includes cancellation and failed recovery probes."}
    for name in ("complete-replay-evidence", "worker-resume-evidence"):
        path = base / (name + ".json")
        if path.exists():
            result[name] = read_json(path)
    result = public_metrics(result)
    write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
