"""Test helpers (kept out of conftest so importing them has no side effects)."""


def intents(*specs, clarify=False, question=None) -> dict:
    """Build a classifier JSON reply: intents(("add_item", 0.9), ("retrieve", 0.8, {"query": "x"}, "span"))."""
    out = []
    for spec in specs:
        name, conf, *rest = spec
        entities = rest[0] if rest else {}
        span = rest[1] if len(rest) > 1 else ""
        out.append({"name": name, "confidence": conf, "entities": entities, "raw_span": span})
    return {
        "intents": out,
        "needs_clarification": clarify,
        "clarification_question": question,
        "language": "en",
    }
