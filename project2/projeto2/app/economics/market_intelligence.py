from __future__ import annotations

from app.analyzers.keyword_analyzer import VALUABLE_TERMS

TREND_MULTIPLIERS = {
    "ai": 1.35,
    "cloud": 1.18,
    "data": 1.15,
    "health": 1.12,
    "finance": 1.1,
    "crypto": 0.9,
}

NICHE_BY_TERM = {
    "ai": "ai",
    "cloud": "cloud",
    "data": "data",
    "health": "health",
    "finance": "finance",
    "loan": "finance",
    "insurance": "insurance",
    "crypto": "crypto",
    "shop": "commerce",
    "travel": "travel",
}


class MarketIntelligence:
    def niche_for_domain(self, domain: str) -> str:
        stem = domain.rsplit(".", 1)[0].lower()
        for term, niche in NICHE_BY_TERM.items():
            if term in stem:
                return niche
        return "general"

    def trend_multiplier(self, domain: str) -> float:
        stem = domain.rsplit(".", 1)[0].lower()
        multiplier = 1.0
        for term, value in TREND_MULTIPLIERS.items():
            if term in stem:
                multiplier = max(multiplier, value)
        return multiplier

    def commercial_intent(self, domain: str) -> float:
        stem = domain.rsplit(".", 1)[0].lower()
        best = 0
        for term, points in VALUABLE_TERMS.items():
            if term in stem:
                best = max(best, points)
        return min(1.0, best / 20)

