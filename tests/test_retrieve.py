"""M7 — "what should I focus on" ranks stored items (pure ranking logic)."""
from datetime import datetime, timedelta, timezone

from app.db.models import Item, Tag
from app.services.context import ContextView
from app.services.retrieve import format_ranked, rank_items

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)  # Monday


def make(id, title, *, type="topic", tags=(), time_hint=None, energy=None, age_days=1, status="open", summary=None):
    return Item(
        id=id,
        user_id=1,
        title=title,
        summary=summary,
        type=type,
        status=status,
        time_hint=time_hint,
        energy=energy,
        created_at=NOW - timedelta(days=age_days),
        tags=[Tag(name=t) for t in tags],
    )


def ids(ranked):
    return [r.item.id for r in ranked]


def test_empty_queue():
    ranked, matched = rank_items([], ContextView(), now=NOW)
    assert ranked == [] and matched
    assert "queue is empty" in format_ranked(ranked, query=None, query_matched=True, ctx=ContextView())


def test_done_items_are_never_suggested():
    items = [make(1, "Kafka basics", status="done"), make(2, "Rust ownership")]
    ranked, _ = rank_items(items, ContextView(), now=NOW)
    assert ids(ranked) == [2]


def test_current_focus_dominates():
    items = [
        make(1, "Rust ownership model"),
        make(2, "GCP BigQuery partitioning", tags=["gcp"]),
        make(3, "Sourdough starter"),
    ]
    ranked, _ = rank_items(items, ContextView(current_focus="GCP"), now=NOW)
    assert ids(ranked)[0] == 2
    assert any("focus" in r for r in ranked[0].reasons)


def test_goal_alignment_boosts():
    items = [make(1, "Rust ownership"), make(2, "AWS IAM policies", tags=["aws"])]
    ranked, _ = rank_items(items, ContextView(goals=["pass the AWS SA exam"]), now=NOW)
    assert ids(ranked)[0] == 2
    assert any("goal" in r for r in ranked[0].reasons)


def test_urgent_item_beats_unrelated_someday_item():
    items = [
        make(1, "Read about category theory", time_hint="someday"),
        make(2, "Submit expense report", type="action", time_hint="today"),
    ]
    ranked, _ = rank_items(items, ContextView(), now=NOW)
    assert ids(ranked) == [2, 1]
    assert any("time-sensitive" in r for r in ranked[0].reasons)


def test_low_energy_prefers_low_effort_items():
    items = [
        make(1, "Deep dive Postgres MVCC", energy="high"),
        make(2, "Tidy up README", energy="low"),
    ]
    ranked, _ = rank_items(items, ContextView(energy_pref="low"), now=NOW)
    assert ids(ranked)[0] == 2
    ranked_high, _ = rank_items(items, ContextView(energy_pref="high"), now=NOW)
    assert ids(ranked_high)[0] == 1


def test_query_filters_to_matching_items():
    items = [
        make(1, "Kafka consumer groups", tags=["kafka"]),
        make(2, "Kafka exactly-once semantics"),
        make(3, "Urgent unrelated thing", type="action", time_hint="today"),
    ]
    ranked, matched = rank_items(items, ContextView(), query="kafka", now=NOW)
    assert matched
    assert set(ids(ranked)) == {1, 2}
    text = format_ranked(ranked, query="kafka", query_matched=True, ctx=ContextView())
    assert 'Focus picks for "kafka"' in text


def test_query_without_matches_falls_back_to_overall_top():
    items = [make(1, "Rust ownership"), make(2, "Terraform modules")]
    ranked, matched = rank_items(items, ContextView(), query="kubernetes", now=NOW)
    assert not matched
    assert len(ranked) == 2
    text = format_ranked(ranked, query="kubernetes", query_matched=False, ctx=ContextView())
    assert 'Nothing open matches "kubernetes"' in text


def test_stale_items_resurface_over_identical_new_ones():
    items = [make(1, "Learn Go generics", age_days=1), make(2, "Learn Zig comptime", age_days=40)]
    ranked, _ = rank_items(items, ContextView(), now=NOW)
    assert ids(ranked) == [2, 1]
    assert any("waiting" in r for r in ranked[0].reasons)


def test_actions_outrank_notes_all_else_equal():
    items = [make(1, "Caching idea", type="note"), make(2, "Fix CI", type="action")]
    ranked, _ = rank_items(items, ContextView(), now=NOW)
    assert ids(ranked) == [2, 1]


def test_limit_and_deterministic_tiebreak():
    items = [make(i, f"Item {i}", age_days=3) for i in range(1, 10)]
    ranked, _ = rank_items(items, ContextView(), now=NOW, limit=3)
    assert ids(ranked) == [1, 2, 3]


def test_naive_datetimes_are_handled():
    item = make(1, "Naive")
    item.created_at = datetime(2026, 9, 1)  # SQLite returns naive datetimes
    ranked, _ = rank_items([item], ContextView(), now=NOW)
    assert ids(ranked) == [1]


def test_format_includes_ids_reasons_and_tip():
    items = [make(7, "Submit expense report", type="action", time_hint="today", energy="low")]
    ranked, _ = rank_items(items, ContextView(), now=NOW)
    text = format_ranked(ranked, query=None, query_matched=True, ctx=ContextView())
    assert "1. #7 Submit expense report (action, low energy)" in text
    assert "time-sensitive (today)" in text
    assert "Tip:" in text  # no focus/goals set
    with_ctx = format_ranked(ranked, query=None, query_matched=True, ctx=ContextView(current_focus="x"))
    assert "Tip:" not in with_ctx
