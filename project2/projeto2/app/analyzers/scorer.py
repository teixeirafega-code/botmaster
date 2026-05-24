from __future__ import annotations

import hashlib

from app.analyzers.backlink_checker import BacklinkChecker
from app.analyzers.keyword_analyzer import KeywordAnalyzer
from app.config.settings import Settings
from app.economics.models import ValuationResult
from app.economics.valuation_engine import ValuationEngine
from app.models import DomainCandidate


class DomainScorer:
    def __init__(self, settings: Settings, backlink_checker: BacklinkChecker, keyword_analyzer: KeywordAnalyzer) -> None:
        self.settings = settings
        self.backlink_checker = backlink_checker
        self.keyword_analyzer = keyword_analyzer
        self.valuation_engine = ValuationEngine(settings)

    async def score(self, candidate: DomainCandidate) -> DomainCandidate:
        if candidate.age_years <= 0:
            candidate.age_years = self._estimated_age(candidate.name)
        candidate.backlinks = await self.backlink_checker.backlink_count(candidate.name)
        candidate.google_indexed = await self.backlink_checker.google_indexed(candidate.name)
        candidate.keyword_value = self.keyword_analyzer.score(candidate.name)

        tld = "." + candidate.name.rsplit(".", 1)[-1].lower()
        candidate.extension_points = self.settings.scoring.extension_points.get(tld, 0)

        age_points = min(30, candidate.age_years * 3)
        backlink_points = min(25, candidate.backlinks // 20)
        index_points = 15 if candidate.google_indexed else 0

        candidate.score = min(
            100,
            age_points + backlink_points + index_points + candidate.keyword_value + candidate.extension_points,
        )
        valuation = await self.valuation_engine.value(candidate)
        candidate.score = valuation.score
        return candidate

    async def value(self, candidate: DomainCandidate) -> ValuationResult:
        await self.score(candidate)
        return await self.valuation_engine.value(candidate)

    def _estimated_age(self, domain: str) -> int:
        digest = hashlib.sha256(f"age:{domain}".encode()).hexdigest()
        return int(digest[:2], 16) % 16
