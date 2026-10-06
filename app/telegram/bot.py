from telegram.ext import Application, CommandHandler

from app.config import settings
from app.telegram import handlers


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    return app