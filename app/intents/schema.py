from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

IntentName = Literal[
    "add_item",
    "retrieve",
    "view_queue",
    "list_tags",
    "mark_done",
    "update_context",
    "get_context",
    "search",
    "help",
    "unknown",
]

INTENT_NAMES: frozenset[str] = frozenset(get_args(IntentName))

# Names models sometimes produce instead of ours.
_ALIASES = {
    "add": "add_item",
    "store": "add_item",
    "save": "add_item",
    "create_item": "add_item",
    "focus": "retrieve",
    "recommend": "retrieve",
    "queue": "view_queue",
    "list_items": "view_queue",
    "tags": "list_tags",
    "done": "mark_done",
    "complete": "mark_done",
    "set_context": "update_context",
    "context": "get_context",
    "find": "search",
}


class Intent(BaseModel):
    name: IntentName
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    entities: dict = Field(default_factory=dict)
    raw_span: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, v):
        if not isinstance(v, str):
            return "unknown"
        key = v.strip().lower().lstrip("/").replace("-", "_").replace(" ", "_")
        key = _ALIASES.get(key, key)
        return key if key in INTENT_NAMES else "unknown"

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if 2.0 <= f <= 100.0:  # some models answer in percent
            f = f / 100.0
        return min(max(f, 0.0), 1.0)

    @field_validator("entities", mode="before")
    @classmethod
    def _entities_dict(cls, v):
        return v if isinstance(v, dict) else {}

    @field_validator("raw_span", mode="before")
    @classmethod
    def _span_str(cls, v):
        return v if isinstance(v, str) else ""


class Classification(BaseModel):
    intents: list[Intent] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    language: str = "en"
    source: str = "llm"  # "llm" | "rules" | "fallback"

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v):
        return v if isinstance(v, str) and v else "en"

    @property
    def top(self) -> Intent | None:
        return max(self.intents, key=lambda i: i.confidence) if self.intents else None
