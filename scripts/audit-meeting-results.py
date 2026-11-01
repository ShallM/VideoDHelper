#!/usr/bin/env python3
"""Read existing application artifacts only; never initiate a model request.

Semantic verdicts must be supplied separately, with an explanation for each
frozen checklist item. Structural checks are not an accuracy score.
"""
import argparse
import hashlib
import json
from pathlib import Path


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def usage(events):
    fields = ("inputTokens", "outputTokens", "reasoningTokens", "cachedInputTokens")
    totals = {k: {"knownSubtotal": sum(e.get("usage", {}).get(k, 0) for e in events),
                  "unknownRequests": sum(k not in e.get("usage", {}) for e in events)} for k in fields}
    money = {}
    for e in events:
        u, p = e.get("usage", {}), e.get("pricing", {})
        c = money.setdefault(p.get("currency", "unknown"), {"knownSubtotal": 0., "unknownRequests": 0,
                                                            "boundedEstimateLower": 0., "boundedEstimateUpper": 0., "unboundedRequests": 0})
        if u.get("invalid") or any(k not in u for k in ("inputTokens", "outputTokens")) or any(p.get(k) is None for k in ("inputPerMillion", "outputPerMillion")):
            c["unknownRequests"] += 1; c["unboundedRequests"] += 1
            continue
        i, o, ir, out = u["inputTokens"], u["outputTokens"], p["inputPerMillion"], p["outputPerMillion"]
        cr = p.get("cachedInputPerMillion")
        cr = ir if cr is None else cr
        cache = u.get("cachedInputTokens")
        if cache is None and cr != ir:
            c["unknownRequests"] += 1
            lower, upper = (i * min(cr, ir) + o * out) / 1e6, (i * max(cr, ir) + o * out) / 1e6
        else:
            cache = cache or 0
            lower = upper = ((i - cache) * ir + cache * cr + o * out) / 1e6
            c["knownSubtotal"] += lower
        c["boundedEstimateLower"] += lower; c["boundedEstimateUpper"] += upper
    return {"httpAttempts": len(events), "tokens": totals, "costs": money,
            "note": "Costs use request-time prices. Unknown usage is not zero; bounds exclude unbounded requests. Reasoning is included in output, not charged twice."}


def evaluate(checklist, verdicts):
    entries = {i["id"]: i for i in checklist["items"]}
    reviewed = {v["id"]: v for v in verdicts["items"]}
    if len(reviewed) != len(verdicts["items"]) or set(entries) != set(reviewed):
        raise ValueError("Verdicts must cover every frozen checklist item exactly once")
    for v in reviewed.values():
        if v.get("verdict") not in ("pass", "fail", "uncertain") or not v.get("reason", "").strip():
            raise ValueError("Each semantic verdict needs an explicit result and reason")
    retained = [i for i in entries.values() if i.get("kind", i.get("checkKind")) == "retention"]
    passed = sum(reviewed[i["id"]]["verdict"] == "pass" for i in retained)
    critical = [i["id"] for i in entries.values() if i.get("priority") == "critical" and reviewed[i["id"]]["verdict"] != "pass"]
    return {"confirmedChecklistRetention": {"passed": passed, "denominator": len(retained),
            "ratio": passed / len(retained) if retained else None,
            "missingOrUncertain": [i["id"] for i in retained if reviewed[i["id"]]["verdict"] != "pass"]},
            "criticalNotPassed": critical, "note": "This fixed text checklist is not overall or audio transcription accuracy. Repeated-run and integration gates remain separate."}


def inspect(folder, events):
    job = read(folder / "job.json", {})
    result = {"taskID": folder.name, "state": job.get("state"), "strategy": job.get("dialogueStrategy", "legacy")}
    source = read(folder / "transcript.json", [])
    edits = read(folder / "transcript-edits.json")
    if edits: source = edits["segments"]
    result.update(sourceSHA256=hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                  sourceSegments=len(source), sourceCharacters=sum(len(s["text"]) for s in source))
    bundle = read(folder / "dialogue-results.json", {})
    notes = bundle.get("notes", read(folder / "notes.json", {}))
    rows = bundle.get("condensed", read(folder / "condensed.json", []))
    result["bundleStrategyMatches"] = not bundle or bundle.get("strategy", notes.get("summaryStrategy")) == result["strategy"]
    if notes:
        claims = [c for ch in notes.get("chapters", []) for c in ch.get("claims", [])]
        cited = claims + notes.get("key_points", []) + notes.get("uncertainties", []) + notes.get("action_items", [])
        allowed = {s["id"] for s in source}
        result.update(chapters=len(notes.get("chapters", [])), claims=len(claims), actions=len(notes.get("action_items", [])),
                      uncertainties=len(notes.get("uncertainties", [])),
                      legalSourceIDs=all(i.get("source_ids") and set(i["source_ids"]) <= allowed for i in cited),
                      renderedCharacters=len(notes.get("overview", "")) + sum(len(i["text"]) for i in cited))
    context = read(folder / "meeting-context.json")
    if context and rows:
        parts = [p for u in context["units"] for p in u["parts"]]
        original = {f"part-{p['id']}": p for p in parts}
        output = [p for r in rows for p in r["parts"]]
        intact = [p["id"] for p in output] == list(original)
        for p in output:
            raw = original[p["id"]]; rendered = raw["text"]
            for op in sorted(p.get("punctuationInsertions", []), key=lambda e: e["offset"], reverse=True):
                rendered = rendered[:op["offset"]] + op["text"] + rendered[op["offset"]:]
            intact &= rendered == p["text"] and p["speaker"] == raw["speaker"] and p["spans"] == raw["spans"]
        result["punctuationOnlyWithUnchangedAttribution"] = intact
    report = read(folder / "summary-run.json", {})
    result["lastExecution"] = {k: v for k, v in report.items() if k not in ("identity", "calls", "requestBudgets")}
    result["metering"] = usage([e for e in events if e.get("taskID") == folder.name])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", nargs="+", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    events = [read(p) for p in args.ledger.glob("*.json")]
    results = [inspect(p, events) for p in args.tasks]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"runs": results, "note": "Structural measures only. Semantic evaluation must be completed separately."}, ensure_ascii=False, indent=2))
    print(f"Audited {len(results)} existing task(s); no model calls.")


if __name__ == "__main__": main()
