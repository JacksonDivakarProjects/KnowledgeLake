import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from app.config import settings
from app.db.engine import SessionLocal, engine, init_db

logging.basicConfig(
    level=settings.kl_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("knowledgelake")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
    except Exception as e:  # noqa: BLE001
        # Don't kill the container if DB is flaky — /ready will report 503.
        log.error("init_db failed: %s", e)
    log.info("KnowledgeLake up (module=m2)")
    try:
        yield
    finally:
        await engine.dispose()
        log.info("Shut down cleanly")


app = FastAPI(title="KnowledgeLake", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "knowledgelake", "module": "m2"}


@app.get("/ready")
async def ready():
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.warning("readiness check failed: %s", e)
        raise HTTPException(status_code=503, detail="db not ready")
    return {"status": "ready", "db": "ok"}