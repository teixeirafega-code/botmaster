from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar


F = TypeVar("F", bound=Callable[..., Any])


def retry(
    max_attempts: int = 3,
    initial_delay_seconds: float = 1.0,
    max_delay_seconds: float = 30.0,
    backoff_factor: float = 2.0,
    jitter_seconds: float = 0.25,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay_seconds
            last_error: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_error = exc
                    if attempt == max_attempts:
                        break
                    sleep_for = min(delay, max_delay_seconds) + random.uniform(0, jitter_seconds)
                    time.sleep(sleep_for)
                    delay *= backoff_factor
            assert last_error is not None
            raise last_error

        return wrapper  # type: ignore[return-value]

    return decorator

