"""FastAPI endpoints (lifespan not started → no Telegram connection)."""
import httpx
import pytest

from app import main
from app.config import settings
from tests.helpers import intents


@pytest.fixture
async def api(db, monkeypatch):
    from app.intents import router

    monkeypatch.setattr(router, "_session_factory", lambda: db)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(api):
    r = await api.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "knowledgelake", "module": "m7"}


async def test_ready_with_db(api):
    r = await api.get("/ready")
    assert r.status_code == 200 and r.json()["db"] == "ok"


async def test_debug_classify(api, fake_llm):
    fake_llm.classify["learn kafka"] = intents(("add_item", 0.9))
    r = await api.post("/debug/classify", json={"message": "learn kafka"})
    assert r.status_code == 200
    body = r.json()
    assert body["classification"]["intents"][0]["name"] == "add_item"
    assert body["meta"]["provider"] == "groq"


async def test_debug_extract(api, fake_llm):
    fake_llm.down = True
    r = await api.post("/debug/extract", json={"message": "remind me to email Priya by Friday"})
    assert r.status_code == 200
    assert r.json()["item"]["title"] == "Email Priya"


async def test_debug_message_runs_full_flow(api, fake_llm):
    fake_llm.down = True
    r = await api.post("/debug/message", json={"message": "learn kafka consumer groups", "telegram_id": 5})
    assert r.status_code == 200 and r.json()["reply"].startswith("Saved #1")
    r = await api.post("/debug/message", json={"message": "what should I focus on", "telegram_id": 5})
    assert "kafka" in r.json()["reply"].lower()


async def test_debug_endpoints_require_token_when_secret_set(api, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    r = await api.post("/debug/classify", json={"message": "help"})
    assert r.status_code == 403
    r = await api.post("/debug/classify", json={"message": "help"}, headers={"X-Debug-Token": "s3cret"})
    assert r.status_code == 200


async def test_webhook_rejects_bad_secrets(api, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    assert (await api.post("/webhook/wrong", json={})).status_code == 403
    r = await api.post("/webhook/s3cret", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert r.status_code == 403


async def test_webhook_503_when_bot_not_ready(api, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "")
    monkeypatch.setitem(main.state, "ptb", None)
    assert (await api.post("/webhook/anything", json={"update_id": 1})).status_code == 503
