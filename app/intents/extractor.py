"""M6 — turn a natural-language message into a structured Item.

LLM extraction (Groq 120b → NVIDIA) with strict normalization, falling back to
deterministic heuristics so an `add_item` never fails just because the LLM did.
"""
import logging
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.llm import client as llm
from app.llm.json_utils import parse_json_object
from app.services import text as T

log = logging.getLogger(__name__)

PROMPT_VERSION = "v2"
_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

ItemType = Literal["topic", "action", "note", "resource"]
Energy = Literal["low", "medium", "high"]
MAX_TAGS = 4
MAX_TITLE_CHARS = 120

_TYPE_ALIASES = {
    "task": "action", "todo": "action", "to-do": "action", "chore": "action",
    "learning": "topic", "learn": "topic", "concept": "topic", "skill": "topic",
    "idea": "note", "thought": "note", "observation": "note",
    "link": "resource", "url": "resource", "book": "resource", "article": "resource",
    "video": "resource", "course": "resource", "podcast": "resource", "paper": "resource",
}
_ENERGY_ALIASES = {"med": "medium", "mid": "medium", "moderate": "medium", "easy": "low", "hard": "high"}


class ExtractedItem(BaseModel):
    title: str
    summary: str | None = None
    type: ItemType = "note"
    tags: list[str] = Field(default_factory=list)
    time_hint: str | None = None
    energy: Energy | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        v = " ".join(str(v or "").split()).strip(" .")
        if not v:
            raise ValueError("empty title")
        return v[:MAX_TITLE_CHARS]

    @field_validator("summary", "time_hint", mode="before")
    @classmethod
    def _opt_str(cls, v):
        if v is None:
            return None
        v = " ".join(str(v).split()).strip()
        return v if v and v.lower() not in {"null", "none", "n/a"} else None

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v):
        v = str(v or "note").strip().lower()
        v = _TYPE_ALIASES.get(v, v)
        return v if v in ("topic", "action", "note", "resource") else "note"

    @field_validator("energy", mode="before")
    @classmethod
    def _energy(cls, v):
        if v is None:
            return None
        v = str(v).strip().lower()
        v = _ENERGY_ALIASES.get(v, v)
        return v if v in ("low", "medium", "high") else None

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if isinstance(v, str):
            v = [p for p in v.replace(";", ",").split(",")]
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for t in v:
            n = T.normalize_tag(str(t))
            if n and n not in out:
                out.append(n)
        return out[:MAX_TAGS]


def _render(message: str) -> str:
    return _PROMPT_TEMPLATE.replace("{{MESSAGE}}", message)


def _coerce(raw: str) -> ExtractedItem | None:
    data = parse_json_object(raw)
    if data is None:
        log.warning("extractor returned non-JSON: %r", (raw or "")[:300])
        return None
    if isinstance(data.get("item"), dict):  # {"item": {...}} wrapper
        data = data["item"]
    try:
        return ExtractedItem.model_validate(data)
    except Exception as e:
        log.warning("extractor JSON failed validation: %s | raw=%r", e, (raw or "")[:300])
        return None


def heuristic_extract(message: str, hint: dict | None = None) -> ExtractedItem:
    """Deterministic extraction; also merges any entities the classifier found."""
    hint = hint or {}
    data = {
        "title": hint.get("title") or T.make_title(message),
        "summary": hint.get("summary"),
        "type": hint.get("type") or T.guess_type(message),
        "tags": hint.get("tags") or T.guess_tags(message),
        "time_hint": hint.get("time_hint") or T.detect_time_hint(message),
        "energy": hint.get("energy") or T.guess_energy(message),
    }
    try:
        return ExtractedItem.model_validate(data)
    except Exception:
        return ExtractedItem(title=T.make_title(message) or "Untitled")


def _fill_gaps(item: ExtractedItem, message: str) -> ExtractedItem:
    """LLMs sometimes drop explicit time/energy cues; recover them cheaply."""
    if item.time_hint is None:
        item.time_hint = T.detect_time_hint(message)
    if item.energy is None:
        item.energy = T.guess_energy(message)
    return item


async def extract(message: str, hint: dict | None = None) -> tuple[ExtractedItem, dict]:
    """Always returns an item. meta['provider'] == 'heuristic' when the LLM was skipped/failed."""
    t0 = time.perf_counter()
    message = (message or "").strip()
    meta = {"provider": "heuristic", "model": "rules", "latency_ms": 0, "prompt_version": PROMPT_VERSION}

    item: ExtractedItem | None = None
    if message:
        try:
            text, provider, model = await llm.complete(
                [{"role": "user", "content": _render(message)}],
                fast=False,  # gpt-oss-120b for extraction quality
                json_mode=True,
                temperature=0.1,
            )
            meta.update(provider=provider, model=model)
            item = _coerce(text)
        except Exception as e:
            log.warning("extractor LLM unavailable, using heuristics: %s", e)

    if item is None:
        item = heuristic_extract(message, hint)
        meta.update(provider="heuristic", model="rules")
    else:
        item = _fill_gaps(item, message)

    meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return item, meta
