"""End-to-end message flow: text → classify → route → DB → reply."""
import pytest
from sqlalchemy import func, select

from app.db.models import IntentLog, Item, RawMessage
from app.intents import router
from app.intents.router import FAILURE_REPLY, handle_message
from app.intents.schema import Intent
from tests.helpers import intents

UID = 777


@pytest.fixture
async def say(db, fake_llm):
    """say("text") → reply, through the full pipeline."""
    counter = {"n": 0}

    async def _say(text, *, uid=UID, update_id=None, forced=None):
        counter["n"] += 1
        return await handle_message(uid, text, username="tester", update_id=update_id, forced=forced, session_factory=db)

    return _say


async def _count(db, model, **where):
    async with db() as s:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return (await s.execute(q)).scalar_one()


async def _add(say, fake_llm, msg, **extracted):
    fake_llm.classify[msg] = intents(("add_item", 0.95, {}, msg))
    fake_llm.extract[msg] = {"title": extracted.pop("title"), **extracted}
    return await say(msg)


# ── add_item (M6) ───────────────────────────────────────────────────────────
async def test_add_item_end_to_end(say, fake_llm, db):
    reply = await _add(
        say, fake_llm, "learn kafka consumer groups this weekend",
        title="Kafka consumer groups", type="topic", tags=["kafka", "streaming"], time_hint="this weekend",
    )
    assert reply.startswith("Saved #1: Kafka consumer groups (topic · this weekend)")
    assert "#kafka" in reply and "#streaming" in reply
    async with db() as s:
        item = (await s.execute(select(Item))).scalar_one()
        raw = (await s.execute(select(RawMessage))).scalar_one()
        assert item.raw_message_id == raw.id
        assert raw.text == "learn kafka consumer groups this weekend"
        log = (await s.execute(select(IntentLog))).scalar_one()
        assert log.provider == "groq" and log.predicted["intents"][0]["name"] == "add_item"


async def test_duplicate_add_is_detected(say, fake_llm, db):
    await _add(say, fake_llm, "learn kafka", title="Kafka")
    reply = await _add(say, fake_llm, "learn kafka again", title="kafka")
    assert reply == "Already in your queue: #1 Kafka"
    assert await _count(db, Item) == 1


async def test_add_works_with_llm_completely_down(say, fake_llm, db):
    fake_llm.down = True
    reply = await say("remind me to email Priya about Q3 by Friday")
    assert reply.startswith("Saved #1: Email Priya about Q3 (action · by Friday)")
    async with db() as s:
        log = (await s.execute(select(IntentLog))).scalar_one()
    assert log.provider == "rules"  # fast path; no LLM needed


async def test_plain_statement_with_llm_down_still_saves(say, fake_llm, db):
    fake_llm.down = True
    reply = await say("caching might be our real bottleneck")
    assert reply.startswith("Saved #1:")
    async with db() as s:
        log = (await s.execute(select(IntentLog))).scalar_one()
    assert log.provider == "fallback"


# ── view_queue / list_tags ──────────────────────────────────────────────────
async def test_queue_empty_and_populated(say, fake_llm):
    assert "queue is empty" in await say("show my queue")
    await _add(say, fake_llm, "learn rust", title="Rust ownership", type="topic", tags=["rust"])
    reply = await say("show my queue")
    assert reply.startswith("Open items (1):")
    assert "#1 · topic · Rust ownership (#rust)" in reply


async def test_queue_truncates_long_lists(say, fake_llm, monkeypatch):
    monkeypatch.setattr(router, "QUEUE_LIMIT", 3)
    for i in range(5):
        await _add(say, fake_llm, f"item number {i}", title=f"Item {i}")
    reply = await say("show my queue")
    assert "Open items (5):" in reply and "…and 2 more" in reply


async def test_tags(say, fake_llm):
    assert "No tags yet" in await say("show my tags")
    await _add(say, fake_llm, "kafka thing", title="Kafka thing", tags=["kafka"])
    assert await say("show my tags") == "Tags (open items):\n#kafka (1)"


# ── mark_done ───────────────────────────────────────────────────────────────
async def test_mark_done_by_id_and_title(say, fake_llm, db):
    await _add(say, fake_llm, "learn sql windows", title="SQL window functions")
    await _add(say, fake_llm, "learn rust", title="Rust ownership")
    assert (await say("done #1")).startswith("Marked done: #1 SQL window functions ✓  (1 open)")
    assert (await say("done with rust ownership")).startswith("Marked done: #2 Rust ownership")
    assert await _count(db, Item, status="done") == 2


async def test_mark_done_already_done_and_not_found(say, fake_llm):
    await _add(say, fake_llm, "learn sql windows", title="SQL window functions")
    await say("done #1")
    assert "already done" in await say("done #1")
    assert "Couldn't find an open item matching" in await say("done with kubernetes")


async def test_mark_done_ambiguous_asks_which(say, fake_llm, db):
    await _add(say, fake_llm, "kafka a", title="Kafka consumer groups")
    await _add(say, fake_llm, "kafka b", title="Kafka exactly once")
    reply = await say("done with kafka")
    assert "which one?" in reply and "#1" in reply and "#2" in reply
    assert await _count(db, Item, status="done") == 0


async def test_mark_done_without_target(say, fake_llm):
    msg = "i finished"
    fake_llm.classify[msg] = intents(("mark_done", 0.9))
    assert "Which item is done?" in await say(msg)


# ── context ─────────────────────────────────────────────────────────────────
async def test_update_and_get_context(say):
    assert "Current focus set to: GCP" in await say("I'm focusing on GCP this month")
    assert "Added goal: pass AWS SA exam" in await say("my goal is pass AWS SA exam")
    assert "Energy set to low" in await say("feeling tired today")
    reply = await say("what are my goals")
    assert "Focus: GCP" in reply and "1. pass AWS SA exam" in reply and "Energy: low" in reply


async def test_update_context_from_llm_entities(say, fake_llm):
    msg = "this quarter I want to nail system design"
    fake_llm.classify[msg] = intents(("update_context", 0.9, {"field": "goals", "value": "nail system design"}))
    assert "Added goal: nail system design" in await say(msg)


async def test_update_context_invalid_value_is_friendly(say, fake_llm):
    msg = "set my energy to purple please"
    fake_llm.classify[msg] = intents(("update_context", 0.9, {"field": "energy_pref", "value": "purple"}))
    assert await say(msg) == "Energy should be low, medium or high."


async def test_update_context_without_entities_defaults_to_focus(say, fake_llm):
    msg = "kubernetes deep work"
    fake_llm.classify[msg] = intents(("update_context", 0.9))
    assert "Current focus set to: kubernetes deep work" in await say(msg)


# ── search ──────────────────────────────────────────────────────────────────
async def test_search(say, fake_llm):
    await _add(say, fake_llm, "kafka x", title="Kafka consumer groups", tags=["kafka"])
    await _add(say, fake_llm, "rust x", title="Rust ownership")
    reply = await say("find anything about kafka")
    assert reply.startswith('Found 1 for "kafka":') and "Kafka consumer groups" in reply
    assert 'Nothing found for "terraform"' in await say("search terraform")


async def test_search_includes_done_items_marked(say, fake_llm):
    await _add(say, fake_llm, "kafka x", title="Kafka consumer groups")
    await say("done #1")
    assert "✓" in await say("search kafka")


async def test_search_query_from_llm_entities(say, fake_llm):
    await _add(say, fake_llm, "kafka x", title="Kafka consumer groups")
    msg = "got anything saved re kafka"
    fake_llm.classify[msg] = intents(("search", 0.9, {"query": "kafka"}))
    assert "Kafka consumer groups" in await say(msg)


# ── retrieve (M7) ───────────────────────────────────────────────────────────
async def test_retrieve_empty(say):
    assert "queue is empty" in await say("what should I focus on?")


async def test_retrieve_ranks_by_focus(say, fake_llm):
    await _add(say, fake_llm, "a", title="Sourdough starter", type="note")
    await _add(say, fake_llm, "b", title="BigQuery partitioning", type="topic", tags=["gcp"])
    await _add(say, fake_llm, "c", title="Rust lifetimes", type="topic")
    await say("I'm focusing on GCP")
    reply = await say("what should I focus on?")
    lines = reply.splitlines()
    assert lines[0] == "Here's what I'd focus on:"
    assert lines[1].startswith("1. #2 BigQuery partitioning")
    assert "on your focus (GCP)" in reply


async def test_retrieve_with_query(say, fake_llm):
    await _add(say, fake_llm, "a", title="Kafka consumer groups", tags=["kafka"])
    await _add(say, fake_llm, "b", title="Rust lifetimes")
    reply = await say("what should I study for kafka")
    assert 'Focus picks for "kafka"' in reply
    assert "Kafka consumer groups" in reply and "Rust" not in reply


async def test_retrieve_excludes_done_items(say, fake_llm):
    await _add(say, fake_llm, "a", title="Kafka consumer groups")
    await _add(say, fake_llm, "b", title="Rust lifetimes")
    await say("done #1")
    reply = await say("what's next")
    assert "Rust lifetimes" in reply and "Kafka" not in reply


async def test_retrieve_works_with_llm_down(say, fake_llm):
    await _add(say, fake_llm, "a", title="Kafka consumer groups")
    fake_llm.down = True
    assert "Kafka consumer groups" in await say("what should I focus on")


# ── multi-intent ────────────────────────────────────────────────────────────
async def test_done_then_retrieve_in_one_message(say, fake_llm):
    await _add(say, fake_llm, "a", title="Terraform modules", type="action")
    await _add(say, fake_llm, "b", title="Rust lifetimes")
    msg = "done with terraform, what's next?"
    fake_llm.classify[msg] = intents(
        ("retrieve", 0.9, {}, "what's next?"),
        ("mark_done", 0.92, {"target": "terraform"}, "done with terraform"),
    )
    reply = await say(msg)
    done_part, next_part = reply.split("\n\n", 1)
    assert done_part.startswith("Marked done: #1 Terraform modules")
    assert "Rust lifetimes" in next_part and "Terraform" not in next_part


async def test_add_and_show_queue(say, fake_llm):
    msg = "add learn go generics and show my queue"
    fake_llm.classify[msg] = intents(
        ("add_item", 0.93, {}, "add learn go generics"),
        ("view_queue", 0.9, {}, "show my queue"),
    )
    fake_llm.extract["add learn go generics"] = {"title": "Go generics", "type": "topic"}
    reply = await say(msg)
    assert "Saved #1: Go generics" in reply
    assert "Open items (1):" in reply


async def test_one_failing_intent_does_not_break_others(say, fake_llm, monkeypatch, db):
    async def boom(*a, **k):
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(router.HANDLERS, "list_tags", boom)
    msg = "add go generics and show my tags"
    fake_llm.classify[msg] = intents(("add_item", 0.93, {}, "add go generics"), ("list_tags", 0.9, {}, "show my tags"))
    fake_llm.extract["add go generics"] = {"title": "Go generics"}
    reply = await say(msg)
    assert "Saved #1: Go generics" in reply
    assert 'Couldn\'t complete "list tags"' in reply
    assert await _count(db, Item) == 1  # the successful intent was committed


# ── clarification / help / unknown ──────────────────────────────────────────
async def test_help(say):
    reply = await say("what can you do?")
    assert "/focus" in reply and "/done" in reply


async def test_clarification_question_is_returned_and_nothing_stored(say, fake_llm, db):
    msg = "kafka"
    fake_llm.classify[msg] = intents(("add_item", 0.7), ("search", 0.68))
    reply = await say(msg)
    assert reply.startswith("Did you want me to")
    assert await _count(db, Item) == 0
    assert await _count(db, RawMessage) == 1  # still captured for later


async def test_gibberish_gets_helpful_reply(say, fake_llm):
    fake_llm.down = True
    reply = await say("asdfghjkl")
    assert "/help" in reply


async def test_empty_message(say):
    assert "/help" in await say("   ")


# ── commands (forced intents) ───────────────────────────────────────────────
async def test_forced_intent_skips_classifier(say, fake_llm):
    fake_llm.extract["learn zig"] = {"title": "Zig comptime", "type": "topic"}
    reply = await say("learn zig", forced=Intent(name="add_item", confidence=1.0))
    assert reply.startswith("Saved #1: Zig comptime")
    assert fake_llm.count("classify") == 0


# ── reliability ─────────────────────────────────────────────────────────────
async def test_duplicate_update_id_is_ignored(say, fake_llm, db):
    await _add(say, fake_llm, "learn kafka", title="Kafka")
    fake_llm.classify["learn rust"] = intents(("add_item", 0.95))
    fake_llm.extract["learn rust"] = {"title": "Rust"}
    assert (await say("learn rust", update_id=9001)).startswith("Saved")
    assert await say("learn rust", update_id=9001) is None
    assert await _count(db, Item) == 2


async def test_multi_intent_offline(say, fake_llm):
    await _add(say, fake_llm, "a", title="Terraform modules")
    fake_llm.down = True
    reply = await say("done with terraform and show my queue")
    assert "Marked done: #1 Terraform modules" in reply
    assert "queue is empty" in reply


async def test_users_are_isolated(say, fake_llm):
    await _add(say, fake_llm, "secret", title="Alice secret plan")
    assert "queue is empty" in await say("show my queue", uid=999)
    assert "Nothing found" in await say("search secret", uid=999)
    assert "Couldn't find" in await say("done #1", uid=999)


async def test_database_failure_returns_friendly_reply(fake_llm):
    class BrokenFactory:
        def __call__(self):
            raise ConnectionError("db down")

    reply = await handle_message(1, "show my queue", session_factory=BrokenFactory())
    assert reply == FAILURE_REPLY


async def test_intent_log_failure_does_not_block_reply(say, fake_llm, monkeypatch):
    def broken_log(**kw):
        raise RuntimeError("intent_logs table missing")

    monkeypatch.setattr(router, "IntentLog", broken_log)
    assert "Your context:" in await say("what are my goals")


@pytest.mark.parametrize(
    "message",
    [
        "help", "show my queue", "show my tags", "what should I focus on", "what are my goals",
        "I'm focusing on rust", "my goal is learn go", "feeling great", "done #5", "find kafka",
        "learn something new", "how does raft consensus work?", "asdfghjkl", "🙂", "a" * 5000,
        "done with x and show my queue", "set timezone to Mars/Base",
    ],
)
async def test_every_message_gets_a_reply_with_llm_down(say, fake_llm, message):
    fake_llm.down = True
    reply = await say(message)
    assert isinstance(reply, str) and reply.strip()
    assert reply != FAILURE_REPLY
