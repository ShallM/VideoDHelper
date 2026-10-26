"""One atomic file per HTTP attempt. No credentials, prompts, or response text."""
import math
from pathlib import Path
import time
import uuid

from common import UserError, write_json


def normalize_usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {"invalid": False}

    def number(value):
        if value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 9_000_000_000_000_000 or int(value) != value:
            result["invalid"] = True
            return None
        return int(value)

    inputs = raw.get("prompt_tokens_details") or {}
    outputs = raw.get("completion_tokens_details") or {}
    inputs = inputs if isinstance(inputs, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    result["inputTokens"] = number(raw.get("prompt_tokens", raw.get("input_tokens")))
    result["outputTokens"] = number(raw.get("completion_tokens", raw.get("output_tokens")))
    result["cachedInputTokens"] = number(inputs.get("cached_tokens", raw.get("prompt_cache_hit_tokens")))
    if result["inputTokens"] is None and result["cachedInputTokens"] is not None:
        miss = number(raw.get("prompt_cache_miss_tokens"))
        if miss is not None:
            result["inputTokens"] = miss + result["cachedInputTokens"]
    result["reasoningTokens"] = number(outputs.get("reasoning_tokens"))
    result["reportedTotalTokens"] = number(raw.get("total_tokens"))
    for part, whole in (("cachedInputTokens", "inputTokens"), ("reasoningTokens", "outputTokens")):
        if result[part] is not None and result[whole] is not None and result[part] > result[whole]:
            result["invalid"] = True
    if all(result[k] is not None for k in ("inputTokens", "outputTokens", "reportedTotalTokens")):
        if result["inputTokens"] + result["outputTokens"] != result["reportedTotalTokens"]:
            result["invalid"] = True
    return {k: v for k, v in result.items() if v is not None}


def safe_pricing(value):
    if not isinstance(value, dict):
        return None
    result = {"currency": value.get("currency", "CNY")}
    if result["currency"] not in ("CNY", "USD", "EUR", "HKD", "JPY"):
        return None
    for key in ("inputPerMillion", "outputPerMillion", "cachedInputPerMillion"):
        number = value.get(key)
        if number is not None:
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1_000_000_000:
                return None
            result[key] = number
    return result


def safe_usage(raw):
    """Keep only numeric metering fields in the legacy per-task log, too."""
    normalized = normalize_usage(raw)
    result = {}
    for source, target in (("inputTokens", "prompt_tokens"), ("outputTokens", "completion_tokens"), ("reportedTotalTokens", "total_tokens")):
        if source in normalized:
            result[target] = normalized[source]
    if "cachedInputTokens" in normalized:
        result["prompt_tokens_details"] = {"cached_tokens": normalized["cachedInputTokens"]}
    if "reasoningTokens" in normalized:
        result["completion_tokens_details"] = {"reasoning_tokens": normalized["reasoningTokens"]}
    return result


class UsageLedger:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["usageRoot"]) if config.get("usageRoot") else None

    def begin(self, phase):
        if self.root is None:
            return None
        event = {"schemaVersion": 1, "id": str(uuid.uuid4()).upper(), "timestamp": time.time(),
                 "purpose": "summary", "phase": phase, "status": "in_flight", "usage": {"invalid": False}}
        for field in ("taskID", "taskTitle", "providerID", "providerName", "modelConfigID", "modelName"):
            if isinstance(self.config.get(field), str):
                event[field] = self.config[field]
        event["modelID"] = self.config["model"]
        pricing = safe_pricing(self.config.get("pricing"))
        if pricing is not None:
            event["pricing"] = pricing
        self._save(event)
        return event

    def finish(self, event, status, usage=None, http_status=None, finish_reason=None):
        if event is None:
            return
        event.update(status=status, durationSeconds=round(time.time() - event["timestamp"], 3), usage=normalize_usage(usage))
        if http_status is not None:
            event["httpStatus"] = http_status
        if finish_reason in ("stop", "length", "content_filter", "tool_calls", "function_call"):
            event["finishReason"] = finish_reason
        self._save(event)

    def _save(self, event):
        try:
            write_json(self.root / "usage/events" / (event["id"] + ".json"), event)
        except OSError:
            raise UserError("无法保存用量记录，请检查本机存储空间和权限后继续。") from None
