from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    jitter_seconds: float = 0.25
    timeout_seconds: float = 30.0
    retry_budget_per_minute: int = 20


class CircuitBreakerOpen(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, cooldown_seconds: int = 60) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.failure_count = 0
        self.half_open_in_flight = False
        self.state = CircuitState.CLOSED
        self.opened_at: datetime | None = None
        self.quarantined_until: datetime | None = None

    def allow_request(self) -> bool:
        if self.quarantined_until and datetime.now(UTC) < self.quarantined_until:
            return False
        if self.state != CircuitState.OPEN:
            if self.state == CircuitState.HALF_OPEN and self.half_open_in_flight:
                return False
            self.half_open_in_flight = self.state == CircuitState.HALF_OPEN
            return True
        if self.opened_at and datetime.now(UTC) - self.opened_at >= self.cooldown:
            self.state = CircuitState.HALF_OPEN
            self.half_open_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.opened_at = None
        self.half_open_in_flight = False

    def record_failure(self) -> None:
        self.failure_count += 1
        self.half_open_in_flight = False
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = datetime.now(UTC)
        if self.failure_count >= self.failure_threshold * 3:
            self.quarantined_until = datetime.now(UTC) + self.cooldown * 5


class ResilienceRegistry:
    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._retry_budget: dict[str, tuple[datetime, int]] = {}

    def breaker(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = CircuitBreaker(provider)
        return self._breakers[provider]

    def lock(self, provider: str) -> asyncio.Lock:
        if provider not in self._locks:
            self._locks[provider] = asyncio.Lock()
        return self._locks[provider]

    def consume_retry_budget(self, provider: str, limit: int) -> bool:
        now = datetime.now(UTC)
        window_start, count = self._retry_budget.get(provider, (now, 0))
        if now - window_start >= timedelta(minutes=1):
            window_start, count = now, 0
        if count >= limit:
            self._retry_budget[provider] = (window_start, count)
            return False
        self._retry_budget[provider] = (window_start, count + 1)
        return True

    def states(self) -> dict[str, str]:
        return {name: breaker.state.value for name, breaker in self._breakers.items()}


resilience_registry = ResilienceRegistry()


async def run_resilient(
    provider: str,
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    retry_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    from app.observability.metrics import runtime_status

    policy = policy or RetryPolicy()
    breaker = resilience_registry.breaker(provider)
    provider_lock = resilience_registry.lock(provider)
    async with provider_lock:
        if not breaker.allow_request():
            raise CircuitBreakerOpen(f"Circuit breaker esta aberto para {provider}")

    last_error: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            result = await asyncio.wait_for(operation(), timeout=policy.timeout_seconds)
            async with provider_lock:
                breaker.record_success()
            runtime_status.provider_status[provider] = "healthy"
            return result
        except retry_exceptions as exc:
            last_error = exc
            async with provider_lock:
                breaker.record_failure()
                allowed = breaker.allow_request()
            runtime_status.api_retries += 1
            runtime_status.provider_failures[provider] = runtime_status.provider_failures.get(provider, 0) + 1
            runtime_status.provider_status[provider] = breaker.state.value
            logger.warning(
                "api_retry",
                extra={"event_name": "api_retry", "provider": provider, "attempt": attempt, "severity": "warning"},
            )
            if not resilience_registry.consume_retry_budget(provider, policy.retry_budget_per_minute):
                runtime_status.retry_budget_exhaustions += 1
                logger.error("retry_budget_exhausted", extra={"event_name": "retry_budget_exhausted", "provider": provider})
                break
            if attempt >= policy.attempts or not allowed:
                break
            delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** (attempt - 1)))
            delay += random.uniform(0, policy.jitter_seconds)
            await asyncio.sleep(delay)
    raise last_error or RuntimeError(f"{provider} operation failed without an exception")
