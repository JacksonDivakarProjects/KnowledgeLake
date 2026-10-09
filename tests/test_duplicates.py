"""Re-logging the same topic must not create a second item."""
import pytest
from sqlalchemy import func, select

from app.db.models import Item
from app.intents.router import handle_message
from app.intents.schema import Intent
from app.services.text import title_similarity
from app.services.items import DUPLICATE_THRESHOLD
from tests.helpers import intents


# ── similarity rules ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "a, b",
    [
        ("Kafka consumer groups", "Kafka consumer groups"),
        ("Kafka consumer groups", "KAFKA CONSUMER GROUPS"),
        ("Kafka consumer groups", "kafka consumer groups!!!"),
        ("Kafka consumer groups", "Learn kafka consumer groups"),          # filler verb
        ("Kafka consumer groups", "Kafka consumer group"),                 # plural
        ("Kafka consumer groups", "Kafka consumer groups basics"),         # superset
        ("Kubernetes networking", "Kubernets networking"),                 # typo
        ("Postgres MVCC internals", "Deep dive into Postgres MVCC"),       # framing words
        ("Rust", "Learn Rust"),
        ("Rust", "rust again"),
        ("SQL window functions", "sql   window\nfunctions"),
        ("C++ templates", "c++ templates"),
    ],
)
def test_same_topic_is_duplicate(a, b):
    assert title_similarity(a, b) >= DUPLICATE_THRESHOLD
    assert title_similarity(b, a) >= DUPLICATE_THRESHOLD  # symmetric


@pytest.mark.parametrize(
    "a, b",
    [
        ("Kafka", "Kafka streams"),                                        # broader vs narrower
        ("Email Priya about Q3 dashboard", "Email Priya about Q4 dashboard"),
        ("Fix login bug", "Fix signup bug"),
        ("Part 1 of the course", "Part 2 of the course"),
        ("Item 1", "Item 2"),
        ("Rust ownership", "Go ownership"),
        ("Kafka consumer group rebalancing", "Kafka producer idempotence"),
        ("C++ templates", "C# templates"),
        ("Read DDIA", "Designing Data-Intensive Applications"),            # needs semantics; accept miss
        ("", "Kafka"),
        ("!!!", "???"),
    ],
)
def test_different_topics_are_not_duplicates(a, b):
    assert title_similarity(a, b) < DUPLICATE_THRESHOLD


# ── end-to-end ──────────────────────────────────────────────────────────────
UID = 4242


@pytest.fixture
async def say(db, fake_llm):
    async def _say(text, *, uid=UID, forced=None):
        return await handle_message(uid, text, session_factory=db, forced=forced)

    return _say


async def _items(db, **where):
    async with db() as s:
        q = select(func.count()).select_from(Item)
        for k, v in where.items():
            q = q.where(getattr(Item, k) == v)
        return (await s.execute(q)).scalar_one()


def _script(fake_llm, msg, **extracted):
    fake_llm.classify[msg] = intents(("add_item", 0.95, {}, msg))
    fake_llm.extract[msg] = extracted


async def test_exact_same_message_twice(say, fake_llm, db):
    _script(fake_llm, "learn kafka consumer groups", title="Kafka consumer groups", tags=["kafka"])
    assert (await say("learn kafka consumer groups")).startswith("Saved #1")
    assert (await say("learn kafka consumer groups")).startswith("Already in your queue: #1 Kafka consumer groups")
    assert await _items(db) == 1


async def test_rephrased_message_with_different_llm_title(say, fake_llm, db):
    _script(fake_llm, "learn kafka consumer groups", title="Kafka consumer groups")
    _script(fake_llm, "I want to study Kafka Consumer Groups", title="Kafka Consumer Groups basics")
    await say("learn kafka consumer groups")
    assert "Already in your queue: #1" in await say("I want to study Kafka Consumer Groups")
    assert await _items(db) == 1


async def test_duplicate_detected_with_llm_down(say, fake_llm, db):
    fake_llm.down = True
    await say("add learn kafka consumer groups")
    assert "Already in your queue" in await say("add kafka consumer groups")
    assert "Already in your queue" in await say("remember to learn Kafka consumer group")
    assert await _items(db) == 1


async def test_duplicate_via_add_command(say, fake_llm, db):
    fake_llm.extract["learn rust"] = {"title": "Rust ownership"}
    forced = Intent(name="add_item", confidence=1.0)
    await say("learn rust", forced=forced)
    assert "Already in your queue" in await say("learn rust", forced=forced)
    assert await _items(db) == 1


async def test_relog_with_new_details_updates_existing(say, fake_llm, db):
    _script(fake_llm, "email priya", title="Email Priya about Q3", type="action")
    _script(fake_llm, "email priya by friday", title="Email Priya about Q3", time_hint="by Friday", tags=["work"])
    await say("email priya")
    reply = await say("email priya by friday")
    assert reply.startswith("Already in your queue: #1 Email Priya about Q3")
    assert "updated: time → by Friday; tags + #work" in reply
    async with db() as s:
        item = (await s.execute(select(Item))).scalar_one()
    assert item.time_hint == "by Friday"


async def test_relog_of_done_item_reopens_it(say, fake_llm, db):
    _script(fake_llm, "learn kafka", title="Kafka consumer groups")
    await say("learn kafka")
    await say("done #1")
    reply = await say("learn kafka")
    assert reply.startswith("You finished #1 Kafka consumer groups before - reopened it.")
    assert await _items(db) == 1
    assert await _items(db, status="open") == 1
    assert "Kafka consumer groups" in await say("show my queue")


async def test_similar_but_different_topics_both_saved(say, fake_llm, db):
    _script(fake_llm, "kafka", title="Kafka")
    _script(fake_llm, "kafka streams", title="Kafka streams")
    _script(fake_llm, "q3", title="Email Priya about Q3 dashboard")
    _script(fake_llm, "q4", title="Email Priya about Q4 dashboard")
    for m in ("kafka", "kafka streams", "q3", "q4"):
        assert (await say(m)).startswith("Saved"), m
    assert await _items(db) == 4


async def test_duplicates_are_per_user(say, fake_llm, db):
    _script(fake_llm, "learn kafka", title="Kafka consumer groups")
    assert (await say("learn kafka", uid=1)).startswith("Saved")
    assert (await say("learn kafka", uid=2)).startswith("Saved")
    assert await _items(db) == 2


async def test_duplicate_inside_one_multi_intent_message(say, fake_llm, db):
    msg = "add kafka; add kafka"
    fake_llm.classify[msg] = intents(("add_item", 0.95, {}, "add kafka"), ("add_item", 0.94, {}, "add kafka"))
    fake_llm.extract["add kafka"] = {"title": "Kafka"}
    reply = await say(msg)
    assert reply.startswith("Saved #1: Kafka")
    assert await _items(db) == 1


async def test_relog_does_not_add_near_identical_tags(say, fake_llm, db):
    _script(fake_llm, "a", title="Kafka consumer groups", tags=["consumer-groups", "kafka"])
    _script(fake_llm, "b", title="Kafka consumer group", tags=["consumer-group", "Kafka", "consumer_groups", "streaming"])
    await say("a")
    reply = await say("b")
    assert "tags + #streaming" in reply and "#consumer-group," not in reply
    async with db() as s:
        item = (await s.execute(select(Item))).scalar_one()
    assert sorted(t.name for t in item.tags) == ["consumer-groups", "kafka", "streaming"]
