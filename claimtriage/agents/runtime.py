"""Per-run execution context shared by the agents: loop bounds, step/tool
counters, cost accounting, and the event log from which a whole run can be
reconstructed (every tool call, query, passage, prompt version, per-step
latency and cost, verification outcome - D5).

Also the clients for the two downstream services. Every call has a timeout;
retriever calls retry with exponential backoff; LLM calls go through the
gateway, which owns provider retries and the circuit breaker.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from ..common import store
from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.resilience import RetryableError, retry_async
from ..common.telemetry import current_trace_id, set_attrs, tracer
from . import metrics as M
from .prompts import Prompt
from .protocol import parse_json_object, wrap_state

log = get_logger(__name__)


class BudgetExceeded(Exception):
    """Step, tool-call or wall-clock bound reached."""


class LLMUnavailable(Exception):
    """Gateway reports the provider is unavailable or the circuit is open."""


@dataclass
class RunContext:
    run_id: str
    correlation_id: str
    agent: str
    deadline: float  # epoch seconds (wall-clock bound for the whole run)
    max_steps: int
    max_tool_calls: int
    overrides: dict = field(default_factory=dict)
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_retries: int = 0
    by_step: dict = field(default_factory=dict)
    prompt_versions: dict = field(default_factory=dict)

    def headers(self) -> dict:
        return {"x-correlation-id": self.correlation_id, "x-run-id": self.run_id}

    def time_left(self) -> float:
        return self.deadline - time.time()

    def check_budget(self, *, tool: bool = False) -> None:
        if self.time_left() <= 0:
            raise BudgetExceeded("wall_clock")
        if self.steps >= self.max_steps:
            raise BudgetExceeded("max_steps")
        if tool and self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("max_tool_calls")

    async def event(self, kind: str, name: str, **data) -> None:
        await store.append_event(self.run_id, {"kind": kind, "agent": self.agent, "name": name, "step": self.steps,
                                               "trace_id": current_trace_id(), **data})

    def cost_summary(self) -> dict:
        return {"total_usd": round(self.cost_usd, 6), "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "by_step": self.by_step}


_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(get_settings().tool_timeout_s, connect=3.0))
    return _http


class RetrieverClient:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.base = get_settings().retriever_url.rstrip("/")

    async def _call(self, method: str, path: str, **kw) -> dict:
        async def once():
            try:
                r = await http().request(method, self.base + path, headers=self.ctx.headers(), **kw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise RetryableError(f"retriever transport: {type(exc).__name__}") from exc
            if r.status_code >= 500:
                raise RetryableError(f"retriever http {r.status_code}")
            if r.status_code == 404:
                return {"error": r.json().get("detail", "not found")}
            r.raise_for_status()
            return r.json()

        def on_retry(n, exc):
            M.RETRIES.labels(self.ctx.agent, "tool").inc()

        return await retry_async(once, attempts=3, base_delay_s=0.2, on_retry=on_retry)

    async def search(self, query: str, k: int = 5, section: str | None = None) -> dict:
        body = {"query": query, "k": k, "mode": self.ctx.overrides.get("retrieval_mode", ""),
                "filters": {"section": section} if section else {},
                "use_cache": not self.ctx.overrides.get("no_cache", False)}
        return await self._call("POST", "/search", json=body)

    async def clause(self, clause_id: str) -> dict:
        return await self._call("GET", f"/clause/{clause_id}")

    async def modifiers(self, clause_id: str) -> dict:
        return await self._call("POST", "/modifiers", json={"clause_id": clause_id})


async def llm_json(ctx: RunContext, prompt: Prompt, state: dict, *, task: str, step: str) -> dict:
    """One model call through the gateway; returns the parsed JSON object.
    Records tokens, cost, latency and the prompt version against this step."""
    settings = get_settings()
    ctx.prompt_versions[prompt.id] = prompt.version
    messages = prompt.render(wrap_state(state))
    t0 = time.perf_counter()
    with tracer().start_as_current_span(f"{ctx.agent}.llm.{step}") as span:
        set_attrs(span, run_id=ctx.run_id, task=task, **prompt.ref)
        try:
            r = await http().post(settings.llm_gateway_url.rstrip("/") + "/v1/complete", headers=ctx.headers(),
                                  timeout=httpx.Timeout(min(settings.llm_call_timeout_s, max(1.0, ctx.time_left())),
                                                        connect=3.0),
                                  json={"task": task, "messages": messages, "max_tokens": prompt.max_tokens,
                                        "meta": {"run_id": ctx.run_id, "agent": ctx.agent, "step": step,
                                                 **prompt.ref}})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            await ctx.event("llm", step, ok=False, error=f"gateway transport: {type(exc).__name__}", **prompt.ref)
            raise LLMUnavailable(f"gateway unreachable: {type(exc).__name__}") from exc
        latency = time.perf_counter() - t0
        if r.status_code >= 500:  # gateway/provider failure of any kind -> degraded path, never a crash
            detail = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            await ctx.event("llm", step, ok=False, error=detail, latency_ms=round(latency * 1000), **prompt.ref)
            raise LLMUnavailable(str(detail.get("error", r.status_code)) if isinstance(detail, dict) else str(detail))
        if r.status_code == 422:  # provider rejected the model's output: treat as an unparseable reply, retry the step
            await ctx.event("llm", step, ok=False, error=r.json().get("detail"), latency_ms=round(latency * 1000),
                            **prompt.ref)
            raise ValueError("model output rejected by provider (invalid JSON)")
        r.raise_for_status()
        data = r.json()
        usage = data["usage"]
        ctx.cost_usd += data["cost_usd"]
        ctx.prompt_tokens += usage["prompt_tokens"]
        ctx.completion_tokens += usage["completion_tokens"]
        ctx.llm_retries += data.get("retries", 0)
        if data.get("retries"):
            M.RETRIES.labels(ctx.agent, "llm").inc(data["retries"])
        agg = ctx.by_step.setdefault(f"{ctx.agent}.{step}", {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0,
                                                             "completion_tokens": 0})
        agg["calls"] += 1
        agg["cost_usd"] = round(agg["cost_usd"] + data["cost_usd"], 8)
        agg["prompt_tokens"] += usage["prompt_tokens"]
        agg["completion_tokens"] += usage["completion_tokens"]
        set_attrs(span, prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
                  cost_usd=data["cost_usd"])
        M.STEP_LAT.labels(ctx.agent, f"llm.{step}").observe(latency)
        try:
            parsed = parse_json_object(data["content"])
            ok = True
        except ValueError:
            parsed, ok = {}, False
        await ctx.event("llm", step, ok=ok, task=task, latency_ms=round(latency * 1000, 1),
                        prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
                        cost_usd=data["cost_usd"], provider=data.get("provider"), retries=data.get("retries", 0),
                        output=parsed if ok else data["content"][:2000], **prompt.ref)
        if not ok:
            raise ValueError("model returned no JSON object")
        return parsed
