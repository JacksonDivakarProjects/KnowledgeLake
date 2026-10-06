import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import text
from telegram import Update

from app.config import settings
from app.db.engine import SessionLocal, engine, init_db
from app.telegram.bot import build_application

logging.basicConfig(
    level=settings.kl_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("knowledgelake")

state: dict = {"ptb": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB
    try:
        await init_db()
    except Exception as e:  # noqa: BLE001
        log.error("init_db failed: %s", e)

    # Telegram
    ptb = build_application()
    await ptb.initialize()
    await ptb.start()

    if settings.kl_mode == "polling":
        await ptb.updater.start_polling(drop_pending_updates=False)
        log.info("Telegram polling started")
    elif settings.kl_mode == "webhook":
        if settings.webhook_url:
            await ptb.bot.set_webhook(
                url=settings.webhook_url,
                secret_token=settings.webhook_secret or None,
                drop_pending_updates=False,
            )
            log.info("Telegram webhook registered: %s", settings.webhook_url)
        else:
            log.warning("KL_MODE=webhook but WEBHOOK_URL is empty — not registering")
    else:
        raise RuntimeError(f"Unknown KL_MODE: {settings.kl_mode}")

    state["ptb"] = ptb
    log.info("KnowledgeLake up (module=m3)")

    try:
        yield
    finally:
        if settings.kl_mode == "polling" and ptb.updater.running:
            await ptb.updater.stop()
        await ptb.stop()
        await ptb.shutdown()
        await engine.dispose()
        log.info("Shut down cleanly")


app = FastAPI(title="KnowledgeLake", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "knowledgelake", "module": "m3"}


@app.get("/ready")
async def ready():
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.warning("readiness check failed: %s", e)
        raise HTTPException(status_code=503, detail="db not ready")
    return {"status": "ready", "db": "ok"}


@app.post("/webhook/{secret}")
async def webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if settings.webhook_secret:
        if secret != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="bad path secret")
        if x_telegram_bot_api_secret_token != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="bad header secret")

    ptb = state.get("ptb")
    if ptb is None:
        raise HTTPException(status_code=503, detail="bot not ready")

    data = await request.json()
    update = Update.de_json(data, ptb.bot)
    await ptb.update_queue.put(update)
    return {"ok": True}