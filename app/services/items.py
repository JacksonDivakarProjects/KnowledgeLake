"""Item persistence, lookup and fuzzy matching."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item
from app.intents.extractor import ExtractedItem
from app.services import text as T
from app.services.tags import upsert_tags

# Personal knowledge base: a user's items comfortably fit in memory for scoring.
SCAN_LIMIT = 2000
_ID_RE = re.compile(r"^#?(\d+)$")


@dataclass
class Match:
    item: Item
    score: float


def item_tokens(item: Item) -> set[str]:
    return T.token_set(item.title, item.summary, " ".join(t.name for t in item.tags))


async def create_item(
    session: AsyncSession, user_id: int, extracted: ExtractedItem, raw_message_id: int | None = None
) -> Item:
    tag_rows = await upsert_tags(session, user_id, extracted.tags)
    item = Item(
        user_id=user_id,
        raw_message_id=raw_message_id,
        title=extracted.title,
        summary=extracted.summary,
        type=extracted.type,
        time_hint=extracted.time_hint,
        energy=extracted.energy,
        status="open",
        created_at=datetime.now(timezone.utc),
        tags=tag_rows,
    )
    session.add(item)
    await session.flush()
    return item


async def find_duplicate(session: AsyncSession, user_id: int, title: str) -> Item | None:
    """An open item with the same title (case-insensitive)."""
    return (
        await session.execute(
            select(Item)
            .where(Item.user_id == user_id, Item.status == "open", func.lower(Item.title) == title.lower())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_items(session: AsyncSession, user_id: int, status: str | None = "open", limit: int = SCAN_LIMIT) -> list[Item]:
    q = select(Item).where(Item.user_id == user_id)
    if status:
        q = q.where(Item.status == status)
    q = q.order_by(Item.created_at.desc(), Item.id.desc()).limit(limit)
    return list((await session.execute(q)).scalars().all())


async def count_items(session: AsyncSession, user_id: int, status: str = "open") -> int:
    return int(
        (await session.execute(
            select(func.count(Item.id)).where(Item.user_id == user_id, Item.status == status)
        )).scalar_one()
    )


async def get_item(session: AsyncSession, user_id: int, item_id: int) -> Item | None:
    item = await session.get(Item, item_id)
    return item if item is not None and item.user_id == user_id else None


def score_match(target: str, item: Item) -> float:
    """How well `target` (free text) names `item`. 0..1."""
    t = " ".join(target.lower().split())
    title = item.title.lower()
    if not t:
        return 0.0
    if t == title:
        return 1.0
    if len(t) >= 3 and (t in title or title in t):
        return 0.9
    return T.overlap_score(T.token_set(target), item_tokens(item)) * 0.85


async def find_matches(session: AsyncSession, user_id: int, target: str, status: str = "open") -> list[Match]:
    """Items matching `target`, best first. '#12' / '12' resolves by id."""
    target = (target or "").strip()
    m = _ID_RE.match(target)
    if m:
        item = await get_item(session, user_id, int(m.group(1)))
        return [Match(item, 1.0)] if item is not None and (status is None or item.status == status) else []

    candidates = await list_items(session, user_id, status=status)
    scored = [Match(i, score_match(target, i)) for i in candidates]
    return sorted((s for s in scored if s.score >= 0.5), key=lambda s: (-s.score, s.item.id))


def mark_done(item: Item) -> None:
    item.status = "done"
    item.completed_at = datetime.now(timezone.utc)


def reopen(item: Item) -> None:
    item.status = "open"
    item.completed_at = None


async def search(session: AsyncSession, user_id: int, query: str, limit: int = 10) -> list[Match]:
    """Keyword search over title/summary/tags across open and done items."""
    q_tokens = T.token_set(query)
    q_lower = " ".join((query or "").lower().split())
    if not q_tokens and not q_lower:
        return []
    items = await list_items(session, user_id, status=None)
    results: list[Match] = []
    for item in items:
        score = T.overlap_score(q_tokens, item_tokens(item)) if q_tokens else 0.0
        haystack = f"{item.title} {item.summary or ''}".lower()
        if q_lower and len(q_lower) >= 2 and q_lower in haystack:
            score = max(score, 0.95)
        if score >= 0.5:
            if item.status == "open":
                score += 0.05  # prefer open items on ties
            results.append(Match(item, score))
    results.sort(key=lambda m: (-m.score, -m.item.id))
    return results[:limit]


def format_item_line(item: Item, *, show_status: bool = False) -> str:
    bits = [f"#{item.id}", item.type, item.title]
    extra = []
    if item.time_hint:
        extra.append(item.time_hint)
    if item.energy:
        extra.append(f"{item.energy} energy")
    if item.tags:
        extra.append(" ".join(f"#{t.name}" for t in item.tags))
    line = " · ".join(bits)
    if extra:
        line += f" ({'; '.join(extra)})"
    if show_status and item.status != "open":
        line += " ✓"
    return line
