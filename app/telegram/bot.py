from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.config import settings
from app.telegram import handlers

COMMANDS = [
    ("start", "wake the bot", handlers.start),
    ("help", "what I can do", handlers.help_cmd),
    ("add", "store something", handlers.add_cmd),
    ("queue", "list open items", handlers.queue_cmd),
    ("focus", "what to work on next", handlers.focus_cmd),
    ("done", "mark an item done", handlers.done_cmd),
    ("search", "find saved items", handlers.search_cmd),
    ("tags", "tags in use", handlers.tags_cmd),
    ("context", "your focus & goals", handlers.context_cmd),
    ("setfocus", "set current focus", handlers.setfocus_cmd),
    ("goal", "add a goal", handlers.goal_cmd),
    ("energy", "set energy low/medium/high", handlers.energy_cmd),
]


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc, _ in COMMANDS])
    except Exception:  # command menu is cosmetic; never block startup on it
        pass


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()
    for name, _, fn in COMMANDS:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.free_text))
    app.add_error_handler(handlers.on_error)
    return app
