import pytest

from app.core.resilience import CircuitBreaker, CircuitBreakerOpen, RetryPolicy, run_resilient


@pytest.mark.asyncio
async def test_run_resilient_retries_then_succeeds():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary")
        return "ok"

    result = await run_resilient("test_retry_provider", flaky, policy=RetryPolicy(attempts=2, base_delay_seconds=0))
    assert result == "ok"
    assert calls == 2


def test_circuit_breaker_opens_after_failures():
    breaker = CircuitBreaker("test", failure_threshold=2)
    breaker.record_failure()
    assert breaker.allow_request() is True
    breaker.record_failure()
    assert breaker.allow_request() is False


@pytest.mark.asyncio
async def test_run_resilient_blocks_open_circuit():
    async def failing():
        raise OSError("down")

    with pytest.raises(OSError):
        await run_resilient("test_open_provider", failing, policy=RetryPolicy(attempts=5, base_delay_seconds=0))
    with pytest.raises(CircuitBreakerOpen):
        await run_resilient("test_open_provider", failing, policy=RetryPolicy(attempts=1, base_delay_seconds=0))

