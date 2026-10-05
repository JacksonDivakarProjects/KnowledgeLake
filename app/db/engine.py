import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models import Base

log = logging.getLogger(__name__)


def _normalize_url(raw: str) -> tuple[str, dict]:
    """asyncpg needs `ssl=require` and rejects `sslmode`/`channel_binding`."""
    if raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(raw)
    qs = parse_qs(parsed.query)

    connect_args: dict = {}
    sslmode = qs.pop("sslmode", [None])[0]
    qs.pop("channel_binding", None)
    if sslmode in ("require", "verify-ca", "verify-full"):
        connect_args["ssl"] = "require"

    flat_qs = {k: v[0] for k, v in qs.items()}
    new_url = urlunparse(parsed._replace(query=urlencode(flat_qs)))
    return new_url, connect_args


_url, _connect_args = _normalize_url(settings.database_url)

engine = create_async_engine(
    _url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=2,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    if not settings.kl_auto_create_schema:
        log.info("KL_AUTO_CREATE_SCHEMA=false — skipping schema init")
        return
    log.info("Creating schema if missing…")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Schema ready")