from __future__ import annotations

import re

VALUABLE_TERMS = {
    "ai": 20,
    "crypto": 18,
    "loan": 18,
    "finance": 16,
    "insurance": 16,
    "health": 14,
    "cloud": 14,
    "data": 13,
    "app": 12,
    "shop": 12,
    "media": 10,
    "travel": 10,
}


class KeywordAnalyzer:
    def score(self, domain: str) -> int:
        stem = domain.rsplit(".", 1)[0].lower()
        tokens = [token for token in re.split(r"[^a-z0-9]+", stem) if token]
        compact = "".join(tokens)

        value = 0
        for term, points in VALUABLE_TERMS.items():
            if term in tokens or term in compact:
                value = max(value, points)

        if 4 <= len(compact) <= 10:
            value += 4
        if compact.isalpha() and len(compact) <= 12:
            value += 3
        if any(char.isdigit() for char in compact):
            value -= 3
        if len(compact) > 18:
            value -= 5
        return max(0, min(20, value))

