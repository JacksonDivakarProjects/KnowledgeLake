"""Shared fixtures.

Environment is pinned *before* any `app` import so Settings never picks up the
real .env (Neon URL, real API keys). No test touches the network: the LLM
transport is replaced with one that fails loudly, and most tests swap
`app.llm.client.complete` for a scripted FakeLLM.
"""
import json
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="kl-tests-"))
os.environ.update(
    {
        # KL_TEST_DATABASE_URL=postgresql://... runs the suite against real Postgres.
        "DATABASE_URL": os.environ.get("KL_TEST_DATABASE_URL")
        or f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}",
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "WEBHOOK_SECRET": "",
        "WEBHOOK_URL": "",
        "GROQ_API_KEY": "test-groq-key",
        "NVIDIA_API_KEY": "test-nvidia-key",
        "KL_LLM_PROVIDER_ORDER": "groq,nvidia",
        "KL_CLASSIFY_CONFIDENCE_THRESHOLD": "0.6",
        "KL_AUTO_CREATE_SCHEMA": "true",
        "KL_TZ": "Asia/Kolkata",
        "KL_RETRIEVE_LIMIT": "5",
        "KL_LLM_TIMEOUT_S": "5",
    }
)

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.db import engine as db_engine  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.llm import client as llm_client  # noqa: E402


# ── SQLite: make SAVEPOINT (session.begin_nested) work under aiosqlite ──────
if db_engine.engine.dialect.name == "sqlite":

    @event.listens_for(db_engine.engine.sync_engine, "connect")
    def _sqlite_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(db_engine.engine.sync_engine, "begin")
    def _sqlite_begin(conn):
        conn.exec_driver_sql("BEGIN")


# ── network guard ───────────────────────────────────────────────────────────
def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected real HTTP call to {request.url}")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    monkeypatch.setattr(llm_client, "_transport", httpx.MockTransport(_no_network))


# ── database ────────────────────────────────────────────────────────────────
@pytest.fixture
async def db():
    async with db_engine.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield db_engine.SessionLocal
    await db_engine.engine.dispose()


@pytest.fixture
async def session(db):
    async with db() as s:
        yield s


@pytest.fixture
async def user(session):
    from app.services.users import get_or_create_user

    u = await get_or_create_user(session, 1001, "tester")
    await session.commit()
    return u


# ── fake LLM ────────────────────────────────────────────────────────────────
class FakeLLM:
    """Scripted stand-in for app.llm.client.complete.

    - `classify[msg]` / `extract[msg]`: dict (JSON-encoded) or str (raw) replies
    - `down = True`: every call raises LLMError (provider outage)
    - unscripted messages raise LLMError too, exercising the fallbacks
    """

    def __init__(self):
        self.classify: dict[str, object] = {}
        self.extract: dict[str, object] = {}
        self.down = False
        self.calls: list[dict] = []

    @staticmethod
    def _message_of(prompt: str) -> str:
        return prompt.rsplit("## User message", 1)[-1].strip()

    async def __call__(self, messages, *, fast=False, json_mode=False, **kw):
        prompt = messages[-1]["content"]
        kind = "classify" if "intent classifier" in prompt else "extract"
        msg = self._message_of(prompt)
        self.calls.append({"kind": kind, "message": msg, "fast": fast, "json_mode": json_mode})
        if self.down:
            raise llm_client.LLMError("All LLM providers failed: simulated outage")
        table = self.classify if kind == "classify" else self.extract
        if msg not in table:
            raise llm_client.LLMError(f"FakeLLM has no scripted {kind} reply for {msg!r}")
        reply = table[msg]
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return text, "groq", "openai/gpt-oss-20b" if fast else "openai/gpt-oss-120b"

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c["kind"] == kind)


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm_client, "complete", fake)
    return fake

