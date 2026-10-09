"""M7 — "what should I focus on": rank open items against the user's context.

Scoring is deterministic and explainable (each pick comes with reasons):

    score = type_weight
          + 1.2 * query relevance        (only when the user named a topic)
          + 1.0 * focus relevance        (UserContext.current_focus)
          + 0.6 * best goal relevance    (UserContext.goals)
          + 0.8 * urgency(time_hint)
          + energy fit                   (UserContext.energy_pref vs Item.energy)
          + 0.2 * staleness              (older open items resurface slowly)
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db.models import Item
from app.services import text as T
from app.services.context import ContextView

TYPE_WEIGHT = {"action": 0.30, "topic": 0.25, "resource": 0.15, "note": 0.10}
W_QUERY = 1.2
W_FOCUS = 1.0
W_GOAL = 0.6
W_URGENCY = 0.8
W_STALE = 0.2
STALE_DAYS = 30


@dataclass
class Ranked:
    item: Item
    score: float
    reasons: list[str] = field(default_factory=list)
    query_hit: bool = False


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _energy_fit(pref: str | None, item_energy: str | None) -> tuple[float, str | None]:
    if not pref or not item_energy:
        return 0.0, None
    if pref == item_energy:
        return 0.25, f"fits your {pref} energy"
    if pref == "low" and item_energy == "high":
        return -0.3, None
    if pref == "low" and item_energy == "medium":
        return -0.1, None
    if pref == "high" and item_energy == "low":
        return -0.05, None
    return 0.0, None


def score_item(
    item: Item,
    ctx: ContextView,
    *,
    query_tokens: set[str],
    focus_tokens: set[str],
    goal_tokens: list[tuple[str, set[str]]],
    now: datetime,
) -> Ranked:
    tokens = T.token_set(item.title, item.summary, " ".join(t.name for t in item.tags))
    score = TYPE_WEIGHT.get(item.type, 0.1)
    reasons: list[str] = []

    query_rel = T.overlap_score(query_tokens, tokens) if query_tokens else 0.0
    if query_rel:
        score += W_QUERY * query_rel
        reasons.append("matches your question")

    focus_rel = T.overlap_score(focus_tokens, tokens) if focus_tokens else 0.0
    if focus_rel:
        score += W_FOCUS * focus_rel
        reasons.append(f"on your focus ({ctx.current_focus})")

    best_goal, best_goal_rel = None, 0.0
    for goal, g_tokens in goal_tokens:
        rel = T.overlap_score(g_tokens, tokens)
        if rel > best_goal_rel:
            best_goal, best_goal_rel = goal, rel
    if best_goal_rel:
        score += W_GOAL * best_goal_rel
        reasons.append(f"supports goal: {best_goal}")

    urg = T.urgency(item.time_hint, now)
    if urg:
        score += W_URGENCY * urg
        if urg >= 0.5:
            reasons.append(f"time-sensitive ({item.time_hint})")

    e_bonus, e_reason = _energy_fit(ctx.energy_pref, item.energy)
    score += e_bonus
    if e_reason:
        reasons.append(e_reason)

    created = _aware(item.created_at)
    if created is not None:
        age_days = max(0.0, (now - created).total_seconds() / 86400)
        score += W_STALE * min(age_days / STALE_DAYS, 1.0)
        if age_days >= 14:
            reasons.append(f"waiting {int(age_days)} days")

    if not reasons and item.type == "action":
        reasons.append("actionable next step")

    return Ranked(item=item, score=round(score, 4), reasons=reasons, query_hit=query_rel > 0)


def rank_items(
    items: list[Item],
    ctx: ContextView,
    *,
    query: str | None = None,
    now: datetime | None = None,
    limit: int = 5,
) -> tuple[list[Ranked], bool]:
    """Return (top items, query_matched).

    When a query is given and at least one item matches it, only matching items
    are returned. Otherwise the overall top items are returned and
    query_matched is False so the caller can say so.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    open_items = [i for i in items if i.status == "open"]
    query_tokens = T.token_set(query)
    focus_tokens = T.token_set(ctx.current_focus)
    goal_tokens = [(g, T.token_set(g)) for g in ctx.goals if T.token_set(g)]

    ranked = [
        score_item(i, ctx, query_tokens=query_tokens, focus_tokens=focus_tokens, goal_tokens=goal_tokens, now=now)
        for i in open_items
    ]
    query_matched = True
    if query_tokens:
        hits = [r for r in ranked if r.query_hit]
        if hits:
            ranked = hits
        else:
            query_matched = False

    ranked.sort(key=lambda r: (-r.score, _aware(r.item.created_at) or now, r.item.id))
    return ranked[: max(1, limit)], query_matched


def format_ranked(ranked: list[Ranked], *, query: str | None, query_matched: bool, ctx: ContextView) -> str:
    if not ranked:
        return "Your queue is empty - send me a topic, task or note and I'll rank it for you."

    lines: list[str] = []
    if query and not query_matched:
        lines.append(f"Nothing open matches \"{query}\". Here's what's top overall:")
    elif query:
        lines.append(f"Focus picks for \"{query}\":")
    else:
        lines.append("Here's what I'd focus on:")

    for n, r in enumerate(ranked, 1):
        i = r.item
        meta = f"{i.type}"
        if i.energy:
            meta += f", {i.energy} energy"
        lines.append(f"{n}. #{i.id} {i.title} ({meta})")
        if r.reasons:
            lines.append(f"   ↳ {'; '.join(r.reasons[:3])}")

    if not ctx.current_focus and not ctx.goals:
        lines.append('\nTip: tell me your focus ("I\'m focusing on GCP") for sharper picks.')
    lines.append('Say "done with #<id>" when you finish one.')
    return "\n".join(lines)
