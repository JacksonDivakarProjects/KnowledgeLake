import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
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

MODULE = "m7"
state: dict = {"ptb": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Bounded so a slow/unreachable DB can't exceed Cloud Run's startup timeout.
        await asyncio.wait_for(init_db(), timeout=20)
    except Exception as e:
        log.error("init_db failed: %s", e)

    ptb = build_application()
    await ptb.initialize()
    await ptb.start()

    if settings.kl_mode == "polling":
        await ptb.updater.start_polling(drop_pending_updates=False)
        log.info("Telegram polling started")
    elif settings.kl_mode == "webhook":
        if settings.webhook_url:
            # Never let a bad WEBHOOK_URL stop the server from listening on PORT;
            # Cloud Run would report it only as "failed to start".
            try:
                await ptb.bot.set_webhook(
                    url=settings.webhook_url,
                    secret_token=settings.webhook_secret or None,
                    drop_pending_updates=False,
                )
                log.info("Telegram webhook registered: %s", settings.webhook_url)
            except Exception as e:
                log.error("set_webhook failed for %r: %s", settings.webhook_url, e)
        else:
            log.warning("KL_MODE=webhook but WEBHOOK_URL is empty - not registering")
    else:
        raise RuntimeError(f"Unknown KL_MODE: {settings.kl_mode}")

    state["ptb"] = ptb
    log.info("KnowledgeLake up (module=%s)", MODULE)

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
    return {"status": "ok", "service": "knowledgelake", "module": MODULE}


@app.get("/ready")
async def ready():
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:
        log.warning("readiness check failed: %s", e)
        raise HTTPException(status_code=503, detail="db not ready")
    return {"status": "ready", "db": "ok"}


class ClassifyIn(BaseModel):
    message: str


class MessageIn(BaseModel):
    message: str
    telegram_id: int


def _check_debug(token: str | None) -> None:
    """Debug endpoints are open locally; gated by WEBHOOK_SECRET when it's set."""
    if settings.webhook_secret and token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="bad debug token")


@app.post("/debug/classify")
async def debug_classify(body: ClassifyIn, x_debug_token: str | None = Header(default=None)):
    _check_debug(x_debug_token)
    from app.intents.classifier import classify
    cls, meta = await classify(body.message)
    return {"classification": cls.model_dump(), "meta": meta}


@app.post("/debug/extract")
async def debug_extract(body: ClassifyIn, x_debug_token: str | None = Header(default=None)):
    _check_debug(x_debug_token)
    from app.intents.extractor import extract
    item, meta = await extract(body.message)
    return {"item": item.model_dump(), "meta": meta}


@app.post("/debug/message")
async def debug_message(body: MessageIn, x_debug_token: str | None = Header(default=None)):
    """Run the full Telegram flow (classify → route → DB) without Telegram."""
    _check_debug(x_debug_token)
    from app.intents.router import handle_message
    reply = await handle_message(body.telegram_id, body.message)
    return {"reply": reply}


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
    # Process inline (not via update_queue): Cloud Run throttles CPU once the
    # response is sent, so background processing would stall and never reply.
    await ptb.process_update(update)
    return {"ok": True}