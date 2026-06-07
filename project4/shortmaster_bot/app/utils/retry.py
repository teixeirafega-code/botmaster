from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar


F = TypeVar("F", bound=Callable[..., Any])


def retry(
    attempts: int = 3,
    delay_seconds: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            wait = delay_seconds
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_error = exc
                    if attempt >= attempts:
                        break
                    logging.getLogger(func.__module__).warning(
                        "%s failed on attempt %s/%s: %s",
                        func.__name__,
                        attempt,
                        attempts,
                        exc,
                    )
                    time.sleep(wait)
                    wait *= backoff
            assert last_error is not None
            raise last_error

        return wrapper  # type: ignore[return-value]

    return decorator
