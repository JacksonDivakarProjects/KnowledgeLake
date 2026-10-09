"""M5 — intent classification.

Pipeline (first one that answers wins):
1. rules.fast_path  — unambiguous messages, no LLM call
2. LLM              — Groq primary, NVIDIA fallback (see app.llm.client)
3. rules.fallback   — every provider failed or returned unusable JSON

`classify` never raises; the worst case is a clarification question.
"""
import logging
import time
from pathlib import Path

from pydantic import ValidationError

from app.config import settings
from app.intents import rules
from app.intents.schema import Classification, Intent
from app.llm import client as llm
from app.llm.json_utils import parse_json_object

log = logging.getLogger(__name__)

PROMPT_VERSION = "v3"
_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify.md"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

# Above this the top intent is trusted even if the model hedged.
CONFIDENT = 0.85
# Two intents closer than this (and both below CONFIDENT) are ambiguous.
AMBIGUITY_MARGIN = 0.1
MAX_MESSAGE_CHARS = 4000

_LABELS = {
    "add_item": "save this as a new item",
    "retrieve": "suggest what to focus on",
    "view_queue": "show your open items",
    "list_tags": "list your tags",
    "mark_done": "mark something as done",
    "update_context": "update your goals/focus",
    "get_context": "show your goals/focus",
    "search": "search your saved items",
    "help": "show help",
    "chitchat": "just chat",
}


def _render(message: str) -> str:
    return _PROMPT_TEMPLATE.replace("{{MESSAGE}}", message)


def _parse(raw: str) -> Classification | None:
    data = parse_json_object(raw)
    if data is None:
        return None
    # Accept {"intent": "...", "confidence": ...} single-intent shape too.
    if "intents" not in data and "intent" in data:
        data = {**data, "intents": [{"name": data.get("intent"), **data}]}
    intents_raw = data.get("intents")
    if not isinstance(intents_raw, list):
        return None

    intents: list[Intent] = []
    for entry in intents_raw:
        if isinstance(entry, str):
            entry = {"name": entry, "confidence": 0.7}
        if not isinstance(entry, dict):
            continue
        try:
            intents.append(Intent.model_validate(entry))
        except ValidationError as e:
            log.debug("dropping malformed intent %r: %s", entry, e)

    try:
        return Classification(
            intents=intents,
            needs_clarification=bool(data.get("needs_clarification", False)),
            clarification_question=data.get("clarification_question") or None,
            language=data.get("language") or "en",
            source="llm",
        )
    except ValidationError as e:
        log.warning("classification envelope invalid: %s", e)
        return None


def _normalize(cls: Classification) -> Classification:
    """Dedupe by name (keep highest confidence) and sort descending."""
    best: dict[str, Intent] = {}
    for i in cls.intents:
        if i.name not in best or i.confidence > best[i.name].confidence:
            best[i.name] = i
    intents = sorted(best.values(), key=lambda i: i.confidence, reverse=True)
    # `unknown` alongside real intents is noise.
    if any(i.name != "unknown" for i in intents):
        intents = [i for i in intents if i.name != "unknown"]
    cls.intents = intents
    return cls


def _clarify_question(cls: Classification) -> str:
    if cls.clarification_question:
        return cls.clarification_question
    named = [i for i in cls.intents if i.name in _LABELS][:2]
    if len(named) == 2:
        return f"Did you want me to {_LABELS[named[0].name]} or {_LABELS[named[1].name]}?"
    if len(named) == 1:
        return f"Just to be sure - should I {_LABELS[named[0].name]}?"
    return (
        "I didn't quite get that. Here's what I can do:\n"
        "• type a topic or task to save it\n"
        "• \"what should I focus on?\" for ranked picks\n"
        "• \"show my queue\" or \"I'm focusing on <topic>\"\n"
        "Or tap a button below / type /help."
    )


def _competing(a: Intent, b: Intent) -> bool:
    """Two intents compete when they claim the same text; distinct spans are a genuine multi-intent."""
    sa, sb = a.raw_span.strip().lower(), b.raw_span.strip().lower()
    if not sa or not sb:
        return True
    return sa == sb or sa in sb or sb in sa


def apply_policy(cls: Classification) -> Classification:
    """Decide whether we're confident enough to act."""
    cls = _normalize(cls)
    threshold = settings.kl_classify_confidence_threshold
    top = cls.intents[0] if cls.intents else None

    if top is None or top.name == "unknown":
        cls.needs_clarification = True
    elif top.confidence >= CONFIDENT:
        # Strong signal: act even if the model hedged.
        cls.needs_clarification = False
    elif top.confidence < threshold:
        cls.needs_clarification = True
    elif (
        len(cls.intents) >= 2
        and cls.intents[1].confidence >= threshold
        and (top.confidence - cls.intents[1].confidence) < AMBIGUITY_MARGIN
        and _competing(top, cls.intents[1])
    ):
        cls.needs_clarification = True

    if cls.needs_clarification:
        cls.clarification_question = _clarify_question(cls)
    else:
        cls.clarification_question = None
    return cls


def actionable(cls: Classification) -> list[Intent]:
    """Intents the router should execute, in confidence order."""
    if cls.needs_clarification:
        return []
    threshold = settings.kl_classify_confidence_threshold
    return [i for i in cls.intents if i.name != "unknown" and i.confidence >= threshold]


async def classify(message: str) -> tuple[Classification, dict]:
    t0 = time.perf_counter()
    message = (message or "").strip()[:MAX_MESSAGE_CHARS]
    meta = {"provider": "rules", "model": "rules", "latency_ms": 0, "prompt_version": PROMPT_VERSION}

    fast = rules.fast_path(message)
    if fast is not None:
        meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return apply_policy(fast), meta

    cls: Classification | None = None
    try:
        text, provider, model = await llm.complete(
            [{"role": "user", "content": _render(message)}],
            fast=True,
            json_mode=True,
            temperature=0.0,
        )
        meta.update(provider=provider, model=model)
        cls = _parse(text)
        if cls is None:
            log.warning("classifier got unparseable output from %s: %r", provider, text[:300])
    except Exception as e:  # LLMError or anything unexpected
        log.warning("classifier LLM unavailable, using rule fallback: %s", e)

    if cls is None or not cls.intents:
        if cls is not None and cls.needs_clarification and cls.clarification_question:
            pass  # model explicitly asked for clarification - respect it
        else:
            cls = rules.fallback(message)
            meta.update(provider="fallback", model="rules")

    meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return apply_policy(cls), meta
