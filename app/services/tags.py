from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item, ItemTag, Tag
from app.services.text import normalize_tag


async def upsert_tags(session: AsyncSession, user_id: int, names: list[str]) -> list[Tag]:
    """Return Tag rows for names (normalized, deduped), creating missing ones."""
    cleaned = sorted({n for n in (normalize_tag(x) for x in names or []) if n})
    if not cleaned:
        return []

    existing = (
        await session.execute(select(Tag).where(Tag.user_id == user_id, Tag.name.in_(cleaned)))
    ).scalars().all()
    have = {t.name: t for t in existing}

    for name in cleaned:
        if name not in have:
            t = Tag(user_id=user_id, name=name)
            session.add(t)
            have[name] = t
    await session.flush()
    return [have[n] for n in cleaned]


async def list_tags_with_counts(session: AsyncSession, user_id: int) -> list[tuple[str, int]]:
    """(tag, open item count) for every tag the user has, most used first."""
    open_count = func.count(Item.id).filter(Item.status == "open")
    rows = (
        await session.execute(
            select(Tag.name, open_count)
            .select_from(Tag)
            .outerjoin(ItemTag, ItemTag.tag_id == Tag.id)
            .outerjoin(Item, Item.id == ItemTag.item_id)
            .where(Tag.user_id == user_id)
            .group_by(Tag.id, Tag.name)
            .order_by(open_count.desc(), Tag.name)
        )
    ).all()
    return [(name, int(count or 0)) for name, count in rows]
