"""Telegram handlers - thin wrappers around app.intents.router.

Free text goes through the classifier; slash commands force a specific intent
so they work even when every LLM provider is down.
"""
import logging

from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.intents.router import FAILURE_REPLY, HELP_TEXT, handle_message
from app.intents.schema import Intent
from app.services.text import chunk_message

log = logging.getLogger(__name__)

START_TEXT = (
    "KnowledgeLake online.\n\n"
    "Dump anything - a topic, a task, a note - and I'll store it.\n"
    "Ask \"what should I focus on?\" and I'll rank what you've saved.\n"
    "Type /help for everything I can do."
)


# Persistent quick-reply menu. Each button just sends its text, which the rules
# fast-path routes without an LLM call (leading emoji is ignored).
MENU_BUTTONS = [
    ["🎯 What should I focus on?", "📋 Show my queue"],
    ["🏷 Show my tags", "🧭 What are my goals"],
    ["❓ Help"],
]
MENU = ReplyKeyboardMarkup(
    MENU_BUTTONS,
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Type a topic, task or question…",
)


async def _reply(update: Update, text: str | None) -> None:
    if not text or update.effective_message is None:
        return
    chunks = chunk_message(text)
    for n, chunk in enumerate(chunks):
        await update.effective_message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=MENU if n == len(chunks) - 1 else None,
        )


async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_chat is not None:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    except Exception:  # cosmetic only
        pass


async def _dispatch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    forced: Intent | None = None,
) -> None:
    user = update.effective_user
    if user is None:
        return
    await _typing(update, context)
    reply = await handle_message(
        user.id,
        text,
        username=user.username,
        display_name=user.first_name,
        update_id=update.update_id,
        forced=forced,
    )
    await _reply(update, reply)


def _args(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


def _command_text(update: Update) -> str:
    return (update.effective_message.text or "").strip() if update.effective_message else ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("telegram /start from user=%s", update.effective_user.id)
    await _reply(update, START_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("telegram /help from user=%s", update.effective_user.id)
    await _reply(update, HELP_TEXT)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _args(context)
    if not text:
        await _reply(update, "Usage: /add <text>  e.g. /add learn SQL window functions")
        return
    await _dispatch(update, context, text, Intent(name="add_item", confidence=1.0, raw_span=text))


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _dispatch(update, context, _command_text(update), Intent(name="view_queue", confidence=1.0))


async def tags_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _dispatch(update, context, _command_text(update), Intent(name="list_tags", confidence=1.0))


async def focus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = _args(context) or None
    await _dispatch(
        update, context, _command_text(update),
        Intent(name="retrieve", confidence=1.0, entities={"query": query} if query else {}),
    )


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _args(context)
    if not target:
        await _reply(update, "Usage: /done <#id or title>  e.g. /done #12")
        return
    await _dispatch(
        update, context, _command_text(update),
        Intent(name="mark_done", confidence=1.0, entities={"target": target}),
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = _args(context)
    if not query:
        await _reply(update, "Usage: /search <words>  e.g. /search dbt")
        return
    await _dispatch(
        update, context, _command_text(update),
        Intent(name="search", confidence=1.0, entities={"query": query}),
    )


async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _dispatch(update, context, _command_text(update), Intent(name="get_context", confidence=1.0))


def _context_setter(field: str, usage: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        value = _args(context)
        if not value:
            await _reply(update, usage)
            return
        await _dispatch(
            update, context, _command_text(update),
            Intent(name="update_context", confidence=1.0, entities={"field": field, "value": value}),
        )

    return handler


setfocus_cmd = _context_setter("current_focus", "Usage: /setfocus <topic>  e.g. /setfocus GCP certification")
goal_cmd = _context_setter("goals", "Usage: /goal <goal>  e.g. /goal pass AWS SA exam by December")
energy_cmd = _context_setter("energy_pref", "Usage: /energy <low|medium|high>")


async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _command_text(update)
    if not text:
        return
    await _dispatch(update, context, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("telegram handler error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(FAILURE_REPLY)
        except Exception:
            pass
