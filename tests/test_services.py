"""Service layer against a real (SQLite) database."""
import pytest

from app.intents.extractor import ExtractedItem
from app.services import context as ctx_svc
from app.services import items as item_svc
from app.services import users as user_svc
from app.services.tags import list_tags_with_counts, upsert_tags


async def _item(session, user, title, **kw):
    item = await item_svc.create_item(session, user.id, ExtractedItem(title=title, **kw))
    await session.commit()
    return item


# ── users / idempotency ─────────────────────────────────────────────────────
async def test_get_or_create_user_is_idempotent_and_updates_username(session):
    a = await user_svc.get_or_create_user(session, 42, "old")
    await session.commit()
    b = await user_svc.get_or_create_user(session, 42, "new")
    await session.commit()
    assert a.id == b.id and b.username == "new"


async def test_processed_update_tracking(session, user):
    assert not await user_svc.already_processed(session, 555)
    await user_svc.mark_processed(session, 555)
    await user_svc.mark_processed(session, 555)  # double mark is harmless
    await session.commit()
    assert await user_svc.already_processed(session, 555)
    assert not await user_svc.already_processed(session, None)


# ── tags ────────────────────────────────────────────────────────────────────
async def test_upsert_tags_normalizes_and_reuses(session, user):
    first = await upsert_tags(session, user.id, ["Kafka", "#kafka", " Data Eng ", ""])
    await session.commit()
    assert [t.name for t in first] == ["data-eng", "kafka"]
    again = await upsert_tags(session, user.id, ["kafka"])
    assert again[0].id == next(t.id for t in first if t.name == "kafka")


async def test_tag_counts_only_count_open_items(session, user):
    a = await _item(session, user, "Kafka basics", tags=["kafka"])
    await _item(session, user, "Kafka streams", tags=["kafka", "streams"])
    item_svc.mark_done(a)
    await session.commit()
    assert await list_tags_with_counts(session, user.id) == [("kafka", 1), ("streams", 1)]


# ── items ───────────────────────────────────────────────────────────────────
async def test_create_and_list_items(session, user):
    a = await _item(session, user, "First", tags=["x"])
    b = await _item(session, user, "Second")
    rows = await item_svc.list_items(session, user.id)
    assert [r.id for r in rows] == [b.id, a.id]
    assert [t.name for t in rows[1].tags] == ["x"]
    assert await item_svc.count_items(session, user.id) == 2


async def test_find_duplicate_is_case_insensitive_and_covers_done_items(session, user):
    a = await _item(session, user, "Learn Kafka")
    assert (await item_svc.find_duplicate(session, user.id, "learn kafka")).id == a.id
    item_svc.mark_done(a)
    await session.commit()
    assert (await item_svc.find_duplicate(session, user.id, "learn kafka")).id == a.id
    assert await item_svc.find_duplicate(session, user.id, "learn kafka", include_done=False) is None


async def test_find_duplicate_prefers_open_item(session, user):
    done = await _item(session, user, "Kafka consumer groups")
    item_svc.mark_done(done)
    await session.commit()
    open_ = await _item(session, user, "Kafka Consumer Groups")
    assert (await item_svc.find_duplicate(session, user.id, "kafka consumer groups")).id == open_.id


async def test_merge_into_fills_gaps_only(session, user):
    a = await _item(session, user, "Kafka consumer groups", tags=["kafka"], energy="low")
    changes = await item_svc.merge_into(
        session, a,
        ExtractedItem(title="x", time_hint="by Friday", energy="high", summary="s", tags=["kafka", "streaming"]),
    )
    assert a.time_hint == "by Friday" and a.energy == "low" and a.summary == "s"
    assert sorted(t.name for t in a.tags) == ["kafka", "streaming"]
    assert changes == ["time → by Friday", "added summary", "tags + #streaming"]
    assert await item_svc.merge_into(session, a, ExtractedItem(title="x")) == []


async def test_find_matches_by_id_and_title(session, user):
    a = await _item(session, user, "SQL window functions", tags=["sql"])
    await _item(session, user, "Rust ownership")
    by_id = await item_svc.find_matches(session, user.id, f"#{a.id}")
    assert by_id[0].item.id == a.id
    by_bare_id = await item_svc.find_matches(session, user.id, str(a.id))
    assert by_bare_id[0].item.id == a.id
    by_title = await item_svc.find_matches(session, user.id, "window functions")
    assert by_title[0].item.id == a.id
    assert await item_svc.find_matches(session, user.id, "kubernetes") == []
    assert await item_svc.find_matches(session, user.id, "#99999") == []


async def test_find_matches_respects_user_isolation(session, user):
    other = await user_svc.get_or_create_user(session, 2002, "other")
    await session.commit()
    theirs = await _item(session, other, "Secret plan")
    assert await item_svc.find_matches(session, user.id, f"#{theirs.id}") == []
    assert await item_svc.find_matches(session, user.id, "secret plan") == []
    assert await item_svc.get_item(session, user.id, theirs.id) is None


async def test_search_covers_title_summary_tags_and_done_items(session, user):
    a = await _item(session, user, "Streaming notes", summary="Kafka consumer group rebalancing")
    b = await _item(session, user, "Ingestion design", tags=["kafka"])
    c = await _item(session, user, "Kafka basics")
    await _item(session, user, "Unrelated")
    item_svc.mark_done(c)
    await session.commit()
    found = [m.item.id for m in await item_svc.search(session, user.id, "kafka")]
    assert set(found) == {a.id, b.id, c.id}
    assert await item_svc.search(session, user.id, "") == []


async def test_mark_done_and_reopen(session, user):
    a = await _item(session, user, "Thing")
    item_svc.mark_done(a)
    assert a.status == "done" and a.completed_at is not None
    item_svc.reopen(a)
    assert a.status == "open" and a.completed_at is None


# ── context ─────────────────────────────────────────────────────────────────
async def test_context_defaults(session, user):
    v = ctx_svc.view(await ctx_svc.get_context(session, user.id))
    assert v.current_focus is None and v.goals == [] and v.timezone == "Asia/Kolkata"
    assert "(not set)" in ctx_svc.format_context(v)


async def test_set_focus_goals_energy_timezone(session, user):
    assert "GCP" in await ctx_svc.update_context(session, user.id, "focus", "GCP")
    assert "Added goal" in await ctx_svc.update_context(session, user.id, "goal", "AWS SA exam")
    assert "Already a goal" in await ctx_svc.update_context(session, user.id, "goals", "aws sa exam")
    await ctx_svc.update_context(session, user.id, "goals", "Ship side project")
    assert "low" in await ctx_svc.update_context(session, user.id, "energy", "pretty tired")
    assert "Europe/London" in await ctx_svc.update_context(session, user.id, "tz", "Europe/London")
    await session.commit()

    v = ctx_svc.view(await ctx_svc.get_context(session, user.id))
    assert v.current_focus == "GCP"
    assert v.goals == ["AWS SA exam", "Ship side project"]
    assert v.energy_pref == "low"
    assert v.timezone == "Europe/London"
    text = ctx_svc.format_context(v)
    assert "1. AWS SA exam" in text and "Tip:" not in text


async def test_goals_persist_across_sessions(db, user):
    async with db() as s:
        await ctx_svc.update_context(s, user.id, "goals", "Goal A")
        await s.commit()
    async with db() as s:
        await ctx_svc.update_context(s, user.id, "goals", "Goal B")
        await s.commit()
    async with db() as s:
        v = ctx_svc.view(await ctx_svc.get_context(s, user.id))
    assert v.goals == ["Goal A", "Goal B"]


async def test_clear_focus_and_goals(session, user):
    await ctx_svc.update_context(session, user.id, "focus", "GCP")
    await ctx_svc.update_context(session, user.id, "goals", "X")
    assert "Cleared" in await ctx_svc.update_context(session, user.id, "focus", "none")
    assert "Cleared" in await ctx_svc.update_context(session, user.id, "goals", "clear")
    v = ctx_svc.view(await ctx_svc.get_context(session, user.id))
    assert v.current_focus is None and v.goals == []


async def test_goal_list_is_capped(session, user):
    for i in range(ctx_svc.MAX_GOALS + 3):
        await ctx_svc.update_context(session, user.id, "goals", f"goal {i}")
    v = ctx_svc.view(await ctx_svc.get_context(session, user.id))
    assert len(v.goals) == ctx_svc.MAX_GOALS
    assert v.goals[-1] == f"goal {ctx_svc.MAX_GOALS + 2}"


@pytest.mark.parametrize(
    "field, value, fragment",
    [
        ("mood", "happy", "I can update"),
        (None, "x", "I can update"),
        ("energy", "purple", "low, medium or high"),
        ("timezone", "Mars/Olympus", "Unknown timezone"),
        ("focus", "", "What should your current focus be"),
        ("goals", "  ", "What goal"),
    ],
)
async def test_context_validation_errors(session, user, field, value, fragment):
    with pytest.raises(ctx_svc.ContextError) as e:
        await ctx_svc.update_context(session, user.id, field, value)
    assert fragment in str(e.value)


def test_timezone_aliases():
    assert ctx_svc.normalize_timezone("IST") == "Asia/Kolkata"
    assert ctx_svc.normalize_timezone("utc") == "UTC"
    assert ctx_svc.normalize_timezone("nope/nope") is None
