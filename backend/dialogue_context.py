"""Lossless reading context for a dialogue, separate from speaker attribution.

Units are transport containers, not claims that a speaker took a new turn.
Character offsets are Python/Unicode code-point offsets into immutable source
segment text. No normalization, punctuation repair, or speaker inference occurs.
"""
import math
import re

from common import validate_segments


VERSION = "dialogue-context-v1"
_ENDING = re.compile(r"[。！？!?；;\n](?:[\"'”’）】)]|\s)*$")
_SPLIT = re.compile(r"[。！？!?；;\n](?:[\"'”’）】)]|\s)*")


def _cut(text, capacity):
    """Prefer a sentence ending without losing the remainder after that ending."""
    if len(text) <= capacity:
        return len(text)
    endings = [m.end() for m in _SPLIT.finditer(text[:capacity]) if m.end() >= capacity // 2]
    return endings[-1] if endings else capacity


def _word_ranges(segment):
    words = segment.get("words", [])
    # Edited text may legitimately retain old word metadata. It must not be used
    # to invent timing for new words, even when the character counts happen to fit.
    if not words or "".join(w["word"] for w in words) != segment["text"]:
        return []
    ranges, offset = [], 0
    for word in words:
        end = offset + len(word["word"])
        if end > offset:
            ranges.append((offset, end, word["start"], word["end"]))
        offset = end
    return ranges


def _time_range(segment, words, start, end):
    matched = [word for word in words if word[0] < end and word[1] > start]
    if not matched:
        # A conservative source interval is honest; proportional interpolation
        # would falsely suggest that the app knows when these characters occurred.
        return segment["start"], segment["end"]
    lower = min(segment["end"], max(segment["start"], min(w[2] for w in matched)))
    upper = min(segment["end"], max(lower, max(w[3] for w in matched)))
    return lower, upper


def build_context(segments, *, max_chars=1200, target_chars=400, max_gap=2.0):
    """Build bounded units with exact text, source slices, and local speaker parts.

    A unit may contain A / unknown / A or a short question and answer. Unknown
    remains unknown and never creates a boundary by itself. Large gaps, sentence
    endings and known speaker changes after the target size provide boundaries;
    max_chars is a hard transport bound. Very large source segments are split
    reversibly. Fragment timing uses matching word evidence when available,
    otherwise retaining the entire source interval; intervals may overlap.
    """
    validate_segments(segments)
    if type(max_chars) is not int or max_chars < 1 or type(target_chars) is not int or target_chars < 1:
        raise ValueError("阅读单元长度必须是正整数")
    if type(max_gap) not in (int, float) or not math.isfinite(max_gap) or max_gap < 0:
        raise ValueError("阅读单元停顿必须是非负有限秒数")
    target_chars = min(target_chars, max_chars)
    units, parts = [], []
    size, part_id = 0, 0

    def flush():
        nonlocal parts, size
        if parts:
            units.append({"id": len(units), "start": min(p["start"] for p in parts),
                          "end": max(p["end"] for p in parts),
                          "text": "".join(p["text"] for p in parts),
                          "source_ids": sorted({s for p in parts for s in p["source_ids"]}),
                          "parts": parts})
        parts, size = [], 0

    def append(segment, word_ranges, begin, end):
        nonlocal size, part_id
        speaker = segment.get("speaker") or "unknown"
        start_time, end_time = _time_range(segment, word_ranges, begin, end)
        span = {"source_id": segment["id"], "start": begin, "end": end}
        content = segment["text"][begin:end]
        if parts and parts[-1]["speaker"] == speaker:
            part = parts[-1]
            part["text"] += content
            part["start"] = min(part["start"], start_time)
            part["end"] = max(part["end"], end_time)
            if part["source_ids"][-1] != segment["id"]:
                part["source_ids"].append(segment["id"])
            previous = part["spans"][-1]
            if previous["source_id"] == span["source_id"] and previous["end"] == span["start"]:
                previous["end"] = span["end"]
            else:
                part["spans"].append(span)
        else:
            parts.append({"id": part_id, "speaker": speaker, "text": content,
                          "start": start_time, "end": end_time,
                          "source_ids": [segment["id"]], "spans": [span]})
            part_id += 1
        size += len(content)

    previous = None
    for segment in segments:
        speaker = segment.get("speaker") or "unknown"
        if parts and previous is not None:
            gap = segment["start"] - previous["end"]
            last_speaker = parts[-1]["speaker"]
            known_change = last_speaker in ("A", "B") and speaker in ("A", "B") and last_speaker != speaker
            if gap > max_gap or (size >= target_chars and known_change):
                flush()
        word_ranges = _word_ranges(segment)
        offset = 0
        while offset < len(segment["text"]):
            capacity = max_chars - size
            end = offset + _cut(segment["text"][offset:], capacity)
            append(segment, word_ranges, offset, end)
            offset = end
            if offset < len(segment["text"]) or size >= max_chars:
                flush()
        if parts and size >= target_chars and _ENDING.search(parts[-1]["text"]):
            flush()
        previous = segment
    flush()
    return units


def source_text(units):
    """Compact model transport; part IDs and local labels, no repeated word JSON."""
    return "\n".join(f"[u{unit['id']}] " + "".join(
        f"[part{part['id']}:{part['speaker']}]" + part["text"] for part in unit["parts"])
        for unit in units)


def groups_by_budget(units, budget, *, estimate):
    """Partition ownership exactly once, using the caller's input estimator.

    This is only a source transport budget. Callers must separately account for
    the complete prompt, draft and response allowance. An oversized unit fails
    explicitly; it is neither silently dropped nor sent beyond this budget.
    """
    if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0 or not callable(estimate):
        raise ValueError("需要有效的阅读预算和规模估算函数")
    groups, group = [], []
    for unit in units:
        cost = estimate(source_text([unit]))
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise ValueError("阅读规模估算无效")
        if cost > budget:
            raise ValueError(f"阅读单元 u{unit['id']} 超过该批输入预算")
        candidate = group + [unit]
        combined = estimate(source_text(candidate))
        if type(combined) not in (int, float) or not math.isfinite(combined) or combined < 0:
            raise ValueError("阅读规模估算无效")
        if group and combined > budget:
            groups.append(group)
            group = [unit]
        else:
            group = candidate
    if group:
        groups.append(group)
    return groups


def select_context(units, unit_ids, radius=1):
    """Select actual neighboring units in source order, without inferring replies."""
    if type(radius) is not int or radius < 0:
        raise ValueError("上下文范围必须是非负整数")
    lookup = {unit["id"]: i for i, unit in enumerate(units)}
    if len(lookup) != len(units):
        raise ValueError("阅读单元编号重复")
    if not isinstance(unit_ids, (list, tuple, set)) or any(type(i) is not int or i not in lookup for i in unit_ids):
        raise ValueError("读取来源超出本任务的阅读单元")
    selected = set()
    for identity in unit_ids:
        index = lookup[identity]
        selected.update(range(max(0, index - radius), min(len(units), index + radius + 1)))
    return [unit for i, unit in enumerate(units) if i in selected]
