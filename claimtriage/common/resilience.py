"""Bounded retry with exponential backoff + jitter, and a circuit breaker (A5)."""
from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """An error worth retrying (timeouts, 5xx, 429)."""


class CircuitOpenError(Exception):
    """Raised without calling the dependency because the breaker is open."""


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_s: float,
    max_delay_s: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (RetryableError,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Call fn up to `attempts` times. Delay = base * 2^(n-1) with full jitter."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:  # type: ignore[misc]
            last = exc
            if attempt == attempts:
                break
            if on_retry:
                on_retry(attempt, exc)
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            await asyncio.sleep(random.uniform(0, delay))
    assert last is not None
    raise last


@dataclass
class CircuitBreaker:
    """Classic three-state breaker.

    closed    -> calls flow; consecutive failures counted
    open      -> calls rejected immediately until reset_s elapses
    half_open -> one trial call allowed; success closes, failure re-opens
    """

    failure_threshold: int = 5
    reset_s: float = 30.0
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0.0
    on_state_change: Callable[[str], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _trial_in_flight: bool = False

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if self.on_state_change:
                self.on_state_change(state)

    def before_call(self) -> None:
        with self._lock:
            if self.state == "open":
                if time.monotonic() - self.opened_at >= self.reset_s:
                    self._set("half_open")
                else:
                    raise CircuitOpenError("circuit open")
            if self.state == "half_open":
                if self._trial_in_flight:
                    raise CircuitOpenError("circuit half-open: trial in flight")
                self._trial_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self._trial_in_flight = False
            self._set("closed")

    def record_failure(self) -> None:
        with self._lock:
            self._trial_in_flight = False
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()
                self._set("open")

    def snapshot(self) -> dict:
        remaining = 0.0
        if self.state == "open":
            remaining = max(0.0, self.reset_s - (time.monotonic() - self.opened_at))
        return {
            "state": self.state,
            "consecutive_failures": self.failures,
            "failure_threshold": self.failure_threshold,
            "reset_s": self.reset_s,
            "seconds_until_half_open": round(remaining, 1),
        }
