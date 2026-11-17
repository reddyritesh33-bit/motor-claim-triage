"""Model providers behind one interface. The provider, endpoint and model id are
chosen by configuration only (LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL), so the
provider can be swapped without a code change (section 12 of the brief)."""
from __future__ import annotations

import asyncio
import json
import random
import re
import ssl
from dataclasses import dataclass

import httpx

from ..common.config import Settings
from ..common.resilience import RetryableError
from .stub import stub_complete


class ProviderError(Exception):
    """Non-retryable provider failure (4xx other than 429, bad response)."""


class InvalidOutputError(ProviderError):
    """The provider rejected the model's own output (e.g. Groq JSON mode:
    json_validate_failed, often a reply cut off at max_tokens). A bad answer, not an
    outage: the agent retries the step; the breaker is not touched."""


class RateLimitedError(RetryableError):
    """HTTP 429: the provider asks us to slow down. Not an outage, so it never
    counts against the circuit breaker; the gateway waits `retry_after` seconds."""

    def __init__(self, msg: str, retry_after: float):
        super().__init__(msg)
        self.retry_after = retry_after


def _retry_after(r: httpx.Response) -> float:
    """Seconds to wait: Retry-After, else Groq's x-ratelimit-reset-tokens (e.g. '12.96s', '1m2s')."""
    for h in ("retry-after", "x-ratelimit-reset-tokens"):
        v = r.headers.get(h)
        if not v:
            continue
        try:
            return float(v)
        except ValueError:
            unit = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
            total = sum(float(n) * unit[u] for n, u in re.findall(r"([\d.]+)(ms|h|m|s)", v))
            if total:
                return total
    return 10.0


@dataclass
class Completion:
    content: str
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class Provider:
    name = "base"

    async def complete(self, messages: list[dict], *, task: str, json_mode: bool, max_tokens: int,
                       temperature: float) -> Completion:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatibleProvider(Provider):
    """Any OpenAI-compatible chat-completions endpoint (Groq free tier in this project)."""

    name = "openai_compatible"

    def __init__(self, s: Settings):
        if not (s.llm_base_url and s.llm_model and s.llm_api_key):
            raise RuntimeError("LLM_BASE_URL, LLM_MODEL and LLM_API_KEY must be set for this provider")
        self.model = s.llm_model
        # provider/model specific options stay in configuration, e.g. {"reasoning_effort": "low"}
        self.extra_body = json.loads(s.llm_extra_body) if s.llm_extra_body.strip() else {}
        self.client = httpx.AsyncClient(base_url=s.llm_base_url.rstrip("/"),
                                        headers={"Authorization": f"Bearer {s.llm_api_key}"},
                                        timeout=httpx.Timeout(s.llm_timeout_s, connect=5.0))

    async def complete(self, messages, *, task, json_mode, max_tokens, temperature) -> Completion:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                **self.extra_body}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await self.client.post("/chat/completions", json=body)
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:  # timeouts, resets, TLS errors on bad networks
            raise RetryableError(f"transport: {type(exc).__name__}") from exc
        if r.status_code == 429:
            raise RateLimitedError(f"http 429: {r.text[:200]}", _retry_after(r))
        if r.status_code >= 500:
            raise RetryableError(f"http {r.status_code}: {r.text[:200]}")
        if r.status_code == 400 and "json_validate_failed" in r.text:
            raise InvalidOutputError(f"http 400 json_validate_failed: {r.text[:300]}")
        if r.status_code >= 400:
            raise ProviderError(f"http {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usage") or {}
        content = data["choices"][0]["message"]["content"] or ""
        return Completion(content=content,
                          prompt_tokens=int(usage.get("prompt_tokens") or estimate_tokens(str(messages))),
                          completion_tokens=int(usage.get("completion_tokens") or estimate_tokens(content)),
                          provider=self.name, model=self.model)


class StubProvider(Provider):
    """Deterministic stand-in for the LLM used by load tests (D6) and offline
    development: fixed, rule-based responses after a realistic delay, so a load
    test measures this system rather than a provider's rate limits."""

    name = "stub"

    def __init__(self, s: Settings):
        self.lo, self.hi = s.stub_delay_ms_min, s.stub_delay_ms_max

    async def complete(self, messages, *, task, json_mode, max_tokens, temperature) -> Completion:
        await asyncio.sleep(random.uniform(self.lo, self.hi) / 1000.0)
        content = stub_complete(task, messages)
        prompt = "".join(m.get("content", "") for m in messages)
        return Completion(content=content, prompt_tokens=estimate_tokens(prompt),
                          completion_tokens=estimate_tokens(content), provider=self.name, model="stub")


def build_provider(s: Settings) -> Provider:
    if s.llm_provider == "stub":
        return StubProvider(s)
    if s.llm_provider in ("openai_compatible", "groq"):
        return OpenAICompatibleProvider(s)
    raise RuntimeError(f"unknown LLM_PROVIDER {s.llm_provider!r}")
