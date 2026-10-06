import logging

from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

START_TEXT = (
    "KnowledgeLake online.\n\n"
    "Dump anything — a topic, a task, a note — and I'll store it.\n"
    "Type /help for commands."
)

HELP_TEXT = (
    "Commands:\n"
    "/start — wake the bot\n"
    "/help — this message\n\n"
    "More commands land as modules ship."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("telegram /start from user=%s", update.effective_user.id)
    await update.message.reply_text(START_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("telegram /help from user=%s", update.effective_user.id)
    await update.message.reply_text(HELP_TEXT)