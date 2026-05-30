from __future__ import annotations

from dataclasses import dataclass

LEET_MAP = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "$": "s",
        "@": "a",
    }
)


@dataclass(frozen=True)
class TrademarkRisk:
    risky: bool
    brand: str = ""
    reason: str = ""


def detect_trademark_risk(domain: str, brands: list[str]) -> TrademarkRisk:
    stem = domain.rsplit(".", 1)[0].lower()
    normalized = _normalize(stem)
    for brand in brands:
        clean_brand = _normalize(brand)
        if not clean_brand:
            continue
        if clean_brand in normalized:
            return TrademarkRisk(True, clean_brand, "contains_famous_brand")
        if _has_confusing_variation(normalized, clean_brand):
            return TrademarkRisk(True, clean_brand, "confusing_brand_variation")
    return TrademarkRisk(False)


def _normalize(value: str) -> str:
    return "".join(char for char in value.lower().translate(LEET_MAP) if char.isalpha())


def _has_confusing_variation(stem: str, brand: str) -> bool:
    if len(stem) < max(3, len(brand) - 2):
        return False
    max_distance = 1 if len(brand) <= 5 else 2
    min_size = max(3, len(brand) - max_distance)
    max_size = len(brand) + max_distance
    for size in range(min_size, max_size + 1):
        for start in range(0, max(0, len(stem) - size) + 1):
            if _edit_distance(stem[start : start + size], brand, max_distance) <= max_distance:
                return True
    return False


def _edit_distance(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        best = current[0]
        for col, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(min(previous[col] + 1, current[col - 1] + 1, previous[col - 1] + cost))
            best = min(best, current[-1])
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]
