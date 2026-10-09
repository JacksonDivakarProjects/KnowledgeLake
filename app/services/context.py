"""Per-user context: current focus, goals, energy preference, timezone."""
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import UserContext

MAX_GOALS = 10
ENERGY_LEVELS = ("low", "medium", "high")

_FIELD_ALIASES = {
    "focus": "current_focus",
    "current_focus": "current_focus",
    "currentfocus": "current_focus",
    "current focus": "current_focus",
    "goal": "goals",
    "goals": "goals",
    "objective": "goals",
    "objectives": "goals",
    "energy": "energy_pref",
    "energy_pref": "energy_pref",
    "energy_level": "energy_pref",
    "energy level": "energy_pref",
    "timezone": "timezone",
    "time_zone": "timezone",
    "time zone": "timezone",
    "tz": "timezone",
}
_ENERGY_ALIASES = {
    "tired": "low", "exhausted": "low", "drained": "low", "sleepy": "low", "lo": "low",
    "ok": "medium", "okay": "medium", "normal": "medium", "moderate": "medium", "med": "medium", "mid": "medium",
    "energetic": "high", "great": "high", "fresh": "high", "pumped": "high", "hi": "high",
}
_TZ_ALIASES = {"ist": "Asia/Kolkata", "india": "Asia/Kolkata", "utc": "UTC", "gmt": "UTC"}


@dataclass
class ContextView:
    current_focus: str | None = None
    goals: list[str] = field(default_factory=list)
    energy_pref: str | None = None
    timezone: str = settings.kl_tz


def normalize_field(name: str | None) -> str | None:
    if not name:
        return None
    return _FIELD_ALIASES.get(str(name).strip().lower())


def normalize_energy(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    for word in re.findall(r"[a-z]+", v):
        if word in ENERGY_LEVELS:
            return word
        if word in _ENERGY_ALIASES:
            return _ENERGY_ALIASES[word]
    return None


def normalize_timezone(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip()
    v = _TZ_ALIASES.get(v.lower(), v)
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return v


def goals_list(ctx: UserContext | None) -> list[str]:
    if ctx is None or not ctx.goals:
        return []
    g = ctx.goals
    if isinstance(g, dict):
        g = g.get("items", [])
    return [str(x) for x in g if str(x).strip()] if isinstance(g, list) else []


def view(ctx: UserContext | None) -> ContextView:
    if ctx is None:
        return ContextView()
    return ContextView(
        current_focus=ctx.current_focus,
        goals=goals_list(ctx),
        energy_pref=ctx.energy_pref,
        timezone=ctx.timezone or settings.kl_tz,
    )


async def get_context(session: AsyncSession, user_id: int) -> UserContext | None:
    return await session.get(UserContext, user_id)


async def _get_or_create(session: AsyncSession, user_id: int) -> UserContext:
    ctx = await session.get(UserContext, user_id)
    if ctx is None:
        ctx = UserContext(user_id=user_id, timezone=settings.kl_tz, goals={"items": []})
        session.add(ctx)
    return ctx


class ContextError(ValueError):
    """User-facing validation error."""


async def update_context(session: AsyncSession, user_id: int, field_name: str | None, value) -> str:
    """Apply one change and return a confirmation sentence. Raises ContextError."""
    fname = normalize_field(field_name)
    if fname is None:
        raise ContextError("I can update your focus, goals, energy (low/medium/high) or timezone.")
    raw = " ".join(str(value or "").split()).strip(" .")

    if fname == "current_focus":
        if not raw:
            raise ContextError("What should your current focus be?")
        if raw.lower() in {"nothing", "none", "clear", "reset"}:
            ctx = await _get_or_create(session, user_id)
            ctx.current_focus = None
            await session.flush()
            return "Cleared your current focus."
        ctx = await _get_or_create(session, user_id)
        ctx.current_focus = raw[:500]
        await session.flush()
        return f"Current focus set to: {ctx.current_focus}"

    if fname == "goals":
        if not raw:
            raise ContextError("What goal should I add?")
        ctx = await _get_or_create(session, user_id)
        if raw.lower() in {"clear", "reset", "none", "clear goals"}:
            ctx.goals = {"items": []}
            await session.flush()
            return "Cleared your goals."
        goals = goals_list(ctx)
        if raw.lower() in (g.lower() for g in goals):
            return f"Already a goal: {raw}"
        goals = (goals + [raw[:300]])[-MAX_GOALS:]
        ctx.goals = {"items": goals}  # reassign so the JSON change is persisted
        await session.flush()
        return f"Added goal: {raw}  ({len(goals)} total)"

    if fname == "energy_pref":
        level = normalize_energy(raw)
        if level is None:
            raise ContextError("Energy should be low, medium or high.")
        ctx = await _get_or_create(session, user_id)
        ctx.energy_pref = level
        await session.flush()
        return f"Energy set to {level} - I'll weigh suggestions accordingly."

    # timezone
    tz = normalize_timezone(raw)
    if tz is None:
        raise ContextError(f"Unknown timezone {raw!r}. Use an IANA name like Asia/Kolkata or Europe/London.")
    ctx = await _get_or_create(session, user_id)
    ctx.timezone = tz
    await session.flush()
    return f"Timezone set to {tz}."


def format_context(v: ContextView) -> str:
    lines = ["Your context:"]
    lines.append(f"• Focus: {v.current_focus or '(not set)'}")
    if v.goals:
        lines.append("• Goals:")
        lines.extend(f"   {i}. {g}" for i, g in enumerate(v.goals, 1))
    else:
        lines.append("• Goals: (none yet)")
    lines.append(f"• Energy: {v.energy_pref or '(not set)'}")
    lines.append(f"• Timezone: {v.timezone}")
    if not v.current_focus and not v.goals:
        lines.append('\nTip: say "I\'m focusing on <topic>" or "my goal is <goal>".')
    return "\n".join(lines)
