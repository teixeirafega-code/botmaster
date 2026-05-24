from __future__ import annotations

import math
from decimal import Decimal, getcontext
from typing import Iterable

getcontext().prec = 50


def bps_to_decimal(bps: int | float) -> Decimal:
    return Decimal(str(bps)) / Decimal("10000")


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def safe_min(values: Iterable[float]) -> float:
    values_list = list(values)
    if not values_list:
        raise ValueError("safe_min() received empty iterable")
    return min(values_list)


def to_decimal(value: str | int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def is_finite_number(x: float) -> bool:
    return math.isfinite(x)

