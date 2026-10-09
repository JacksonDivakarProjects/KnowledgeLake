"""Unified chat-completion client.

Groq primary (openai/gpt-oss-*), NVIDIA NIM fallback (nvidia/nemotron-3-super-120b-a12b).
Both expose OpenAI-compatible /v1/chat/completions. Provider order comes
from KL_LLM_PROVIDER_ORDER. Missing key => provider skipped with a warning.

Transient failures (connect errors, 429, 5xx, empty replies) are retried once
per provider; read timeouts, auth and other 4xx errors fail over to the next
provider immediately.
"""
import logging
from dataclasses import dataclass

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

log = logging.getLogger(__name__)

# Tests swap this for an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


class LLMError(RuntimeError):
    """Raised when every configured provider failed."""


class EmptyCompletion(RuntimeError):
    """Provider answered 200 but with no usable content."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    default_model: str
    fast_model: str


def _providers() -> dict[str, Provider]:
    return {
        "groq": Provider(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            api_key=settings.groq_api_key,
            default_model=settings.kl_llm_model,
            fast_model=settings.kl_llm_fast_model,
        ),
        "nvidia": Provider(
            name="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=settings.nvidia_api_key,
            default_model=settings.nvidia_model,
            fast_model=settings.nvidia_model,
        ),
    }


def _is_transient(exc: BaseException) -> bool:
    # A read timeout means the model is slow/hung; retrying just doubles the wait.
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, EmptyCompletion)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.kl_llm_timeout_s, transport=_transport)


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.4, max=2),
    reraise=True,
)
async def _call(provider: Provider, model: str, messages: list[dict], **kw) -> str:
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    url = f"{provider.base_url}/chat/completions"
    async with _client() as client:
        r = await client.post(url, headers=headers, json={"model": model, "messages": messages, **kw})
        if r.status_code == 400 and "response_format" in r.text and "response_format" in kw:
            log.info("provider %r rejected response_format - retrying without", provider.name)
            kw = {k: v for k, v in kw.items() if k != "response_format"}
            r = await client.post(url, headers=headers, json={"model": model, "messages": messages, **kw})
        r.raise_for_status()

    try:
        content = r.json()["choices"][0]["message"].get("content")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise EmptyCompletion(f"malformed response from {provider.name}: {e}") from e
    if not content or not content.strip():
        raise EmptyCompletion(f"empty content from {provider.name}/{model}")
    return content


def configured_providers() -> list[str]:
    providers = _providers()
    return [n for n in settings.provider_order if n in providers and providers[n].api_key]


async def complete(
    messages: list[dict],
    *,
    fast: bool = False,
    json_mode: bool = False,
    **kw,
) -> tuple[str, str, str]:
    """Return (text, provider_name, model_name). Raises LLMError if all providers fail."""
    providers = _providers()
    last_err: Exception | None = None

    if json_mode:
        kw.setdefault("response_format", {"type": "json_object"})

    for name in settings.provider_order:
        p = providers.get(name)
        if not p:
            log.warning("LLM provider %r unknown - skipped", name)
            continue
        if not p.api_key:
            log.warning("LLM provider %r skipped (no key configured)", name)
            continue
        model = p.fast_model if fast else p.default_model
        try:
            text = await _call(p, model, messages, **kw)
            return text, p.name, model
        except Exception as e:
            log.warning("LLM provider %r failed: %s: %s", name, type(e).__name__, e)
            last_err = e

    raise LLMError(f"All LLM providers failed: {type(last_err).__name__ if last_err else None}: {last_err}")
