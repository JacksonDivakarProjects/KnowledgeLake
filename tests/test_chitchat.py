"""Greetings, thanks, small talk and off-topic messages: friendly replies that route the user."""
import pytest
from sqlalchemy import func, select

from app.db.models import Item
from app.intents import rules
from app.intents.router import FAILURE_REPLY, handle_message
from app.telegram.handlers import MENU_BUTTONS
from tests.helpers import intents


@pytest.mark.parametrize(
    "message, kind",
    [
        ("hi", "greeting"), ("Hii", "greeting"), ("hello!", "greeting"), ("Hey there", "greeting"),
        ("good morning", "greeting"), ("namaste", "greeting"), ("yo", "greeting"), ("👋 hi", "greeting"),
        ("how are you?", "smalltalk"), ("what's up", "smalltalk"), ("who are you", "smalltalk"),
        ("thanks", "thanks"), ("Thank you so much!", "thanks"), ("thx", "thanks"), ("ty", "thanks"),
        ("bye", "bye"), ("good night", "bye"), ("see you later", "bye"),
        ("ok", "ack"), ("cool", "ack"), ("got it", "ack"), ("lol", "ack"), ("👍", "ack"), ("🙂🙂", "ack"),
    ],
)
def test_chitchat_fast_path(message, kind):
    cls = rules.fast_path(message)
    assert cls is not None, message
    assert cls.intents[0].name == "chitchat"
    assert cls.intents[0].entities == {"kind": kind}


@pytest.mark.parametrize(
    "message, intent, span",
    [
        ("hi, add learn rust", "add_item", "add learn rust"),
        ("hey! show my queue", "view_queue", "show my queue"),
        ("good morning, what should I focus on?", "retrieve", "what should I focus on?"),
        ("Hello there - feeling tired today", "update_context", "feeling tired today"),
    ],
)
def test_greeting_prefix_is_stripped_and_request_handled(message, intent, span):
    cls = rules.fast_path(message)
    assert cls.intents[0].name == intent and cls.intents[0].raw_span == span


@pytest.mark.parametrize(
    "message",
    ["tell me a joke", "what's the weather like in Chennai today?", "who won the cricket match yesterday",
     "write me a poem", "what time is it", "recommend a movie", "are you chatgpt?"],
)
def test_off_topic_detected_without_llm(message):
    for cls in (rules.fast_path(message), rules.fallback(message)):
        assert cls.intents[0].name == "chitchat" and cls.intents[0].entities == {"kind": "off_topic"}


@pytest.mark.parametrize(
    "message",
    ["remember to check the weather before the trip", "add tell me a joke script for the party", "learn weather API"],
)
def test_real_tasks_mentioning_off_topic_words_are_still_saved(message):
    assert rules.fallback(message).intents[0].name == "add_item"


async def test_greeting_plus_request_offline_saves_clean_title(db, fake_llm):
    fake_llm.down = True
    reply = await handle_message(1, "hi! add learn rust ownership", session_factory=db)
    assert reply.startswith("Saved #1: Learn rust ownership"), reply  # no "Hi!" in the title


@pytest.mark.parametrize("message", ["tell me a joke", "what's the weather in Delhi?", "write me a poem"])
async def test_off_topic_never_saved_offline(db, fake_llm, message):
    fake_llm.down = True
    reply = await handle_message(1, message, session_factory=db)
    assert reply.startswith("That's outside what I can help with")
    async with db() as s:
        assert (await s.execute(select(func.count()).select_from(Item))).scalar_one() == 0


@pytest.mark.parametrize("message", ["??", "...", "!!!"])
def test_punctuation_only_is_not_treated_as_friendly(message):
    cls = rules.fallback(message)
    assert cls.intents[0].name == "unknown"


@pytest.mark.parametrize(
    "button, intent",
    [(b, i) for row, intents_ in zip(MENU_BUTTONS, [["retrieve", "view_queue"], ["list_tags", "get_context"], ["help"]])
     for b, i in zip(row, intents_)],
)
def test_every_menu_button_routes_without_llm(button, intent):
    cls = rules.fast_path(button)
    assert cls is not None and cls.intents[0].name == intent


# ── end-to-end replies ──────────────────────────────────────────────────────
@pytest.fixture
async def say(db, fake_llm):
    async def _say(text, name=None):
        return await handle_message(1, text, display_name=name, session_factory=db)

    return _say


async def _count(db):
    async with db() as s:
        return (await s.execute(select(func.count()).select_from(Item))).scalar_one()


async def _seed(say, fake_llm):
    fake_llm.classify["learn kafka"] = intents(("add_item", 0.95))
    fake_llm.extract["learn kafka"] = {"title": "Kafka consumer groups", "type": "topic"}
    await say("learn kafka")


async def test_hi_on_empty_queue_onboards(say, db):
    reply = await say("hi", name="Jack")
    assert reply.startswith("Hi Jack! 👋 Your queue is empty")
    assert "what should I focus on?" in reply
    assert await _count(db) == 0  # greeting must never be saved as an item


async def test_hi_shows_top_pick(say, fake_llm):
    await _seed(say, fake_llm)
    reply = await say("hello")
    assert "You have 1 open item. Top pick right now: #1 Kafka consumer groups." in reply


async def test_thanks_and_bye(say, fake_llm):
    assert await say("thanks") == "Anytime! 🙌"
    await _seed(say, fake_llm)
    assert await say("thank you!") == "Anytime! 🙌 Next up: #1 Kafka consumer groups."
    assert await say("bye") == "See you! 👋 1 item will be waiting."


async def test_smalltalk_and_ack(say):
    assert "Doing great" in await say("how are you?")
    assert (await say("👍")).startswith("👍 Anything else?")


@pytest.mark.parametrize("message", ["hi", "thanks", "ok", "bye", "good morning", "🙂"])
async def test_chitchat_never_saved_even_with_llm_down(say, fake_llm, db, message):
    fake_llm.down = True
    reply = await say(message)
    assert reply and reply != FAILURE_REPLY
    assert not reply.startswith("Saved")
    assert await _count(db) == 0


async def test_off_topic_question_is_politely_redirected(say, fake_llm, db):
    msg = "what's the weather in Chennai today?"
    fake_llm.classify[msg] = intents(("chitchat", 0.93, {"kind": "off_topic"}))
    reply = await say(msg)
    assert reply.startswith("That's outside what I can help with")
    assert "add what's the weather in Chennai today" in reply
    assert await _count(db) == 0


async def test_off_topic_shows_related_saved_items(say, fake_llm):
    await _seed(say, fake_llm)
    msg = "tell me about kafka"
    fake_llm.classify[msg] = intents(("chitchat", 0.9, {"kind": "off_topic"}))
    reply = await say(msg)
    assert "here's what you've saved that looks related" in reply
    assert "Kafka consumer groups" in reply


async def test_greeting_plus_request_just_does_the_request(say, fake_llm, db):
    msg = "hi! add learn rust"
    fake_llm.classify[msg] = intents(("chitchat", 0.9, {"kind": "greeting"}, "hi!"), ("add_item", 0.95, {}, "add learn rust"))
    fake_llm.extract["add learn rust"] = {"title": "Rust ownership"}
    reply = await say(msg)
    assert reply.startswith("Saved #1: Rust ownership")
    assert "👋" not in reply


async def test_chitchat_without_kind_from_llm_defaults_sensibly(say, fake_llm):
    fake_llm.classify["yo bot whats good"] = intents(("chitchat", 0.9))
    reply = await say("yo bot whats good")
    assert reply and reply != FAILURE_REPLY


async def test_unknown_gives_guided_menu(say, fake_llm):
    fake_llm.down = True
    reply = await say("??")
    assert "Here's what I can do" in reply and "what should I focus on?" in reply


async def test_menu_keyboard_is_attached_to_replies(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.telegram import handlers

    async def fake_handle(*a, **k):
        return "hello"

    monkeypatch.setattr(handlers, "handle_message", fake_handle)
    msg = SimpleNamespace(text="hi", reply_text=AsyncMock())
    upd = SimpleNamespace(
        update_id=1, effective_user=SimpleNamespace(id=1, username="u", first_name="J"),
        effective_chat=SimpleNamespace(id=1), effective_message=msg,
    )
    await handlers.free_text(upd, SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock())))
    assert msg.reply_text.await_args.kwargs["reply_markup"] is handlers.MENU
