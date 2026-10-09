"""Telegram layer: commands map to the right intents, replies are chunked, errors are handled."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.intents.router import FAILURE_REPLY, HELP_TEXT
from app.telegram import bot, handlers


def make_update(text="hello", user_id=1, update_id=100):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(
        update_id=update_id,
        effective_user=SimpleNamespace(id=user_id, username="tester", first_name="Jack"),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=message,
        message=message,
    )


def make_context(args=None):
    return SimpleNamespace(args=args or [], bot=SimpleNamespace(send_chat_action=AsyncMock()), error=None)


def replies(update) -> list[str]:
    return [c.args[0] for c in update.effective_message.reply_text.await_args_list]


@pytest.fixture
def routed(monkeypatch):
    """Capture what the handlers send to the router."""
    calls = []

    async def fake_handle(telegram_id, text, *, username=None, display_name=None, update_id=None, forced=None, session_factory=None):
        calls.append({"uid": telegram_id, "text": text, "update_id": update_id, "forced": forced})
        return "ROUTED"

    monkeypatch.setattr(handlers, "handle_message", fake_handle)
    return calls


async def test_start_and_help_reply_directly(routed):
    u = make_update("/start")
    await handlers.start(u, make_context())
    assert "KnowledgeLake online" in replies(u)[0]
    u = make_update("/help")
    await handlers.help_cmd(u, make_context())
    assert replies(u) == [HELP_TEXT]
    assert routed == []


async def test_free_text_goes_through_classifier(routed):
    u = make_update("learn kafka", update_id=55)
    ctx = make_context()
    await handlers.free_text(u, ctx)
    assert routed == [{"uid": 1, "text": "learn kafka", "update_id": 55, "forced": None}]
    assert replies(u) == ["ROUTED"]
    ctx.bot.send_chat_action.assert_awaited()


async def test_free_text_ignores_empty(routed):
    u = make_update("")
    await handlers.free_text(u, make_context())
    assert routed == [] and replies(u) == []


@pytest.mark.parametrize(
    "handler, args, intent, entities",
    [
        (handlers.add_cmd, ["learn", "rust"], "add_item", {}),
        (handlers.queue_cmd, [], "view_queue", {}),
        (handlers.tags_cmd, [], "list_tags", {}),
        (handlers.focus_cmd, [], "retrieve", {}),
        (handlers.focus_cmd, ["kafka"], "retrieve", {"query": "kafka"}),
        (handlers.done_cmd, ["#3"], "mark_done", {"target": "#3"}),
        (handlers.search_cmd, ["dbt", "models"], "search", {"query": "dbt models"}),
        (handlers.context_cmd, [], "get_context", {}),
        (handlers.setfocus_cmd, ["GCP"], "update_context", {"field": "current_focus", "value": "GCP"}),
        (handlers.goal_cmd, ["AWS", "cert"], "update_context", {"field": "goals", "value": "AWS cert"}),
        (handlers.energy_cmd, ["low"], "update_context", {"field": "energy_pref", "value": "low"}),
    ],
)
async def test_commands_force_intents(routed, handler, args, intent, entities):
    u = make_update("/cmd " + " ".join(args))
    await handler(u, make_context(args))
    assert len(routed) == 1
    forced = routed[0]["forced"]
    assert forced.name == intent and forced.confidence == 1.0
    assert forced.entities == entities
    if intent == "add_item":
        assert routed[0]["text"] == "learn rust"


@pytest.mark.parametrize(
    "handler",
    [handlers.add_cmd, handlers.done_cmd, handlers.search_cmd, handlers.setfocus_cmd, handlers.goal_cmd, handlers.energy_cmd],
)
async def test_commands_without_args_show_usage(routed, handler):
    u = make_update("/cmd")
    await handler(u, make_context([]))
    assert routed == []
    assert replies(u)[0].startswith("Usage:")


async def test_duplicate_update_sends_nothing(monkeypatch):
    async def none_reply(*a, **k):
        return None

    monkeypatch.setattr(handlers, "handle_message", none_reply)
    u = make_update("learn kafka")
    await handlers.free_text(u, make_context())
    assert replies(u) == []


async def test_long_replies_are_chunked(monkeypatch):
    async def long_reply(*a, **k):
        return "\n".join("x" * 100 for _ in range(100))  # ~10k chars

    monkeypatch.setattr(handlers, "handle_message", long_reply)
    u = make_update("show my queue")
    await handlers.free_text(u, make_context())
    sent = replies(u)
    assert len(sent) == 3 and all(len(s) <= 4000 for s in sent)


async def test_typing_indicator_failure_is_ignored(routed):
    u = make_update("hi")
    ctx = make_context()
    ctx.bot.send_chat_action = AsyncMock(side_effect=RuntimeError("telegram down"))
    await handlers.free_text(u, ctx)
    assert replies(u) == ["ROUTED"]


async def test_error_handler_ignores_non_update_objects():
    ctx = make_context()
    ctx.error = RuntimeError("boom")
    await handlers.on_error(object(), ctx)  # must not raise


async def test_error_handler_with_real_update(monkeypatch):
    from telegram import Update as TgUpdate

    reply = AsyncMock()
    msg = SimpleNamespace(reply_text=reply)
    monkeypatch.setattr(TgUpdate, "effective_message", property(lambda self: msg))
    upd = TgUpdate(update_id=1)
    ctx = make_context()
    ctx.error = RuntimeError("boom")
    await handlers.on_error(upd, ctx)
    reply.assert_awaited_once_with(FAILURE_REPLY)


async def test_end_to_end_through_real_router(db, fake_llm, monkeypatch):
    from app.intents import router

    monkeypatch.setattr(router, "_session_factory", lambda: db)
    u = make_update("/add learn rust", update_id=1)
    fake_llm.extract["learn rust"] = {"title": "Rust ownership", "type": "topic", "tags": ["rust"]}
    await handlers.add_cmd(u, make_context(["learn", "rust"]))
    assert replies(u)[0].startswith("Saved #1: Rust ownership")

    u2 = make_update("what should I focus on?", update_id=2)
    await handlers.free_text(u2, make_context())
    assert "Rust ownership" in replies(u2)[0]

    u3 = make_update("/done #1", update_id=3)
    await handlers.done_cmd(u3, make_context(["#1"]))
    assert replies(u3)[0].startswith("Marked done: #1")


def test_application_registers_all_commands_and_error_handler():
    app = bot.build_application()
    registered = {
        cmd for group in app.handlers.values() for h in group for cmd in getattr(h, "commands", ())
    }
    assert registered == {name for name, _, _ in bot.COMMANDS}
    assert handlers.on_error in app.error_handlers
