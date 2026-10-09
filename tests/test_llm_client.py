"""Provider order, failover and retry behaviour of app.llm.client (M5: "fallback works")."""
import json

import httpx
import pytest
from tenacity import wait_none

from app.config import settings
from app.llm import client


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


class Router:
    """MockTransport handler: per-host queue of responses, records every request."""

    def __init__(self, **per_host):
        self.queues = {host: list(rs) for host, rs in per_host.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = "groq" if "groq" in request.url.host else "nvidia"
        q = self.queues.get(host) or []
        if not q:
            raise AssertionError(f"no scripted response left for {host}")
        r = q.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def hosts(self) -> list[str]:
        return ["groq" if "groq" in r.url.host else "nvidia" for r in self.requests]


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(client._call.retry, "wait", wait_none())
    monkeypatch.setattr(settings, "groq_api_key", "gk")
    monkeypatch.setattr(settings, "nvidia_api_key", "nk")
    monkeypatch.setattr(settings, "kl_llm_provider_order", "groq,nvidia")

    def install(**per_host) -> Router:
        router = Router(**per_host)
        monkeypatch.setattr(client, "_transport", httpx.MockTransport(router))
        return router

    return install


async def test_groq_primary_success_uses_fast_model_and_json_mode(transport):
    r = transport(groq=[_ok('{"x": 1}')])
    text, provider, model = await client.complete([{"role": "user", "content": "hi"}], fast=True, json_mode=True)
    assert (text, provider, model) == ('{"x": 1}', "groq", settings.kl_llm_fast_model)
    body = json.loads(r.requests[0].content)
    assert body["model"] == settings.kl_llm_fast_model
    assert body["response_format"] == {"type": "json_object"}
    assert r.requests[0].headers["authorization"] == "Bearer gk"


async def test_default_model_when_not_fast(transport):
    transport(groq=[_ok("ok")])
    _, _, model = await client.complete([{"role": "user", "content": "hi"}])
    assert model == settings.kl_llm_model


async def test_transient_5xx_is_retried_on_same_provider(transport):
    r = transport(groq=[httpx.Response(503, text="busy"), _ok("recovered")])
    text, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert (text, provider) == ("recovered", "groq")
    assert r.hosts() == ["groq", "groq"]


async def test_rate_limit_then_failover_to_nvidia(transport):
    r = transport(groq=[httpx.Response(429), httpx.Response(429)], nvidia=[_ok("from nvidia")])
    text, provider, model = await client.complete([{"role": "user", "content": "hi"}])
    assert (text, provider, model) == ("from nvidia", "nvidia", settings.nvidia_model)
    assert r.hosts() == ["groq", "groq", "nvidia"]


async def test_auth_error_fails_over_immediately_without_retry(transport):
    r = transport(groq=[httpx.Response(401, text="bad key")], nvidia=[_ok("fine")])
    _, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert provider == "nvidia"
    assert r.hosts() == ["groq", "nvidia"]


async def test_read_timeout_fails_over_without_retry(transport):
    r = transport(groq=[httpx.ReadTimeout("slow")], nvidia=[_ok("fine")])
    _, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert provider == "nvidia"
    assert r.hosts() == ["groq", "nvidia"]


async def test_connect_error_is_retried(transport):
    r = transport(groq=[httpx.ConnectError("reset"), _ok("second try")])
    text, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert (text, provider) == ("second try", "groq")
    assert r.hosts() == ["groq", "groq"]


async def test_empty_content_counts_as_failure(transport):
    transport(groq=[_ok(""), _ok("   ")], nvidia=[_ok("real answer")])
    text, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert (text, provider) == ("real answer", "nvidia")


async def test_malformed_body_counts_as_failure(transport):
    bad = httpx.Response(200, json={"unexpected": True})
    transport(groq=[bad, bad], nvidia=[_ok("ok")])
    _, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert provider == "nvidia"


async def test_response_format_rejection_retries_without_it(transport):
    r = transport(nvidia=[httpx.Response(400, text="response_format not supported"), _ok('{"a":1}')])
    settings.kl_llm_provider_order = "nvidia"
    text, provider, _ = await client.complete([{"role": "user", "content": "hi"}], json_mode=True)
    assert (text, provider) == ('{"a":1}', "nvidia")
    assert "response_format" in json.loads(r.requests[0].content)
    assert "response_format" not in json.loads(r.requests[1].content)


async def test_missing_key_skips_provider(transport, monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    r = transport(nvidia=[_ok("only nvidia")])
    _, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert provider == "nvidia"
    assert r.hosts() == ["nvidia"]
    assert client.configured_providers() == ["nvidia"]


async def test_unknown_provider_in_order_is_ignored(transport, monkeypatch):
    monkeypatch.setattr(settings, "kl_llm_provider_order", "openai,groq")
    transport(groq=[_ok("groq")])
    _, provider, _ = await client.complete([{"role": "user", "content": "hi"}])
    assert provider == "groq"


async def test_all_providers_fail_raises_llm_error(transport):
    transport(groq=[httpx.Response(500)] * 2, nvidia=[httpx.Response(500)] * 2)
    with pytest.raises(client.LLMError):
        await client.complete([{"role": "user", "content": "hi"}])


async def test_no_keys_at_all_raises_llm_error(transport, monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    transport()
    with pytest.raises(client.LLMError):
        await client.complete([{"role": "user", "content": "hi"}])
