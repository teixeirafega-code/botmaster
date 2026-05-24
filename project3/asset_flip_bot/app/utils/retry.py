from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    handled_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    """Retry a callable with exponential backoff and jitter."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = logging.getLogger(func.__module__)
            last_error: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except handled_exceptions as exc:
                    last_error = exc
                    if attempt == attempts:
                        break
                    sleep_for = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    sleep_for += random.uniform(0, sleep_for * 0.25)
                    logger.warning(
                        "Retrying %s after error on attempt %s/%s: %s",
                        func.__name__,
                        attempt,
                        attempts,
                        exc,
                    )
                    time.sleep(sleep_for)
            assert last_error is not None
            raise last_error

        return wrapper  # type: ignore[return-value]

    return decorator

