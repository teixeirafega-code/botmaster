from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    jitter_seconds: float = 0.2
    retry_on: tuple[type[BaseException], ...] = (Exception,)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    config: RetryConfig = RetryConfig(),
    logger=None,
) -> T:
    last_exc: Optional[BaseException] = None

    for attempt in range(1, config.max_attempts + 1):
        try:
            return await fn()
        except config.retry_on as exc:  # noqa: PERF203
            last_exc = exc
            if attempt >= config.max_attempts:
                break

            delay = min(config.max_delay_seconds, config.base_delay_seconds * (2 ** (attempt - 1)))
            delay += random.uniform(0, config.jitter_seconds)
            if logger:
                logger.warning(
                    "Retrying after error (attempt %s/%s) in %.2fs: %s",
                    attempt,
                    config.max_attempts,
                    delay,
                    exc,
                )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc

