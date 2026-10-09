"""Tolerant JSON extraction from LLM output.

Models occasionally wrap JSON in ```json fences or add a sentence before/after
it even when told not to. `parse_json_object` strips that noise and returns the
first JSON object it can find, or None.
"""
import json
import re

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _balanced_objects(text: str):
    """Yield each top-level {...} span in order, respecting strings/escapes."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break
        start = text.find("{", start + 1)


def parse_json_object(raw: str | None) -> dict | None:
    if not raw:
        return None
    cleaned = raw.strip()

    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    for span in _balanced_objects(cleaned):
        try:
            data = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
