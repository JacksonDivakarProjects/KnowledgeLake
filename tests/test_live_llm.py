"""Opt-in smoke tests against the real providers configured in .env.

    KL_LIVE_TESTS=1 pytest -m live -s

Only LLM calls are made (no DB, no Telegram). Skipped by default.
"""
import asyncio
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.config import settings
from app.intents.classifier import actionable, classify
from app.intents.extractor import extract
from app.llm import client as llm_client

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("KL_LIVE_TESTS") != "1", reason="set KL_LIVE_TESTS=1 to hit real LLMs"),
]

ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")


@pytest.fixture(autouse=True)
async def _respect_rate_limit():
    yield
    await asyncio.sleep(2.5)  # Groq free tier is ~30 req/min


@pytest.fixture(autouse=True)
def real_keys(monkeypatch):
    monkeypatch.setattr(llm_client, "_transport", None)  # lift the network guard
    for key in ("groq_api_key", "nvidia_api_key", "kl_llm_model", "kl_llm_fast_model", "nvidia_model"):
        value = ENV.get(key.upper())
        if value:
            monkeypatch.setattr(settings, key, value)


@pytest.mark.parametrize(
    "message, expected",
    [
        ("learn SQL window functions", {"add_item"}),
        ("what do I have about kafka? and show my tags", {"search", "list_tags"}),
        ("I've wrapped up the terraform refactor", {"mark_done"}),
        ("I'm going to concentrate on system design for the next few weeks", {"update_context"}),
    ],
)
async def test_groq_classifies(message, expected, monkeypatch):
    monkeypatch.setattr(settings, "kl_llm_provider_order", "groq")
    cls, meta = await classify(message)
    print(f"\n{message!r} -> {[(i.name, i.confidence, i.entities) for i in cls.intents]} via {meta}")
    assert meta["provider"] in ("groq", "rules")  # obvious messages never need the LLM
    assert {i.name for i in actionable(cls)} == expected


async def test_nvidia_fallback_classifies(monkeypatch):
    """Groq forced to fail. Passes if NVIDIA answers, or if NVIDIA is down and the rules still answer."""
    if not ENV.get("NVIDIA_API_KEY"):
        pytest.skip("no NVIDIA key")
    monkeypatch.setattr(settings, "groq_api_key", "invalid-key-to-force-fallback")
    monkeypatch.setattr(settings, "kl_llm_timeout_s", 30.0)  # production timeout; gemma-31b is slower
    cls, meta = await classify("learn SQL window functions")
    print(f"\nfallback -> {meta}")
    assert meta["provider"] in ("nvidia", "fallback")
    if meta["provider"] == "fallback":
        print("WARNING: NVIDIA did not answer - check NVIDIA_MODEL; the rule fallback handled it")
    assert [i.name for i in actionable(cls)] == ["add_item"]


async def test_groq_extracts():
    item, meta = await extract("remind me to email Priya about the Q3 dashboard by Friday, quick one")
    print(f"\n{item.model_dump()} via {meta}")
    assert meta["provider"] in ("groq", "nvidia")
    assert item.type == "action"
    assert item.time_hint and "friday" in item.time_hint.lower()


@pytest.mark.parametrize(
    "message, expected",
    [
        ("hey hope you're having a good day!", {"chitchat"}),
        ("what's the weather like in Chennai today?", {"chitchat"}),
        ("tell me a joke", {"chitchat"}),
        ("who won the cricket match yesterday", {"chitchat"}),
        ("hi! add learn rust ownership", {"add_item"}),
        ("remember to check the weather before the trip", {"add_item"}),
    ],
)
async def test_groq_routes_chitchat_and_off_topic(message, expected, monkeypatch):
    monkeypatch.setattr(settings, "kl_llm_provider_order", "groq")
    cls, meta = await classify(message)
    print(f"\n{message!r} -> {[(i.name, i.confidence, i.entities) for i in cls.intents]}")
    assert meta["provider"] in ("groq", "rules")  # obvious messages never need the LLM
    names = {i.name for i in actionable(cls)}
    # the router drops chitchat when a real request is present
    assert (names - {"chitchat"} or names) == expected
