from __future__ import annotations

import math
import re

from app.config.settings import Settings
from app.economics.domain_intelligence import DomainIntelligence, DomainIntelligenceClient
from app.economics.historical_sales import HistoricalSalesIntelligence
from app.economics.market_intelligence import MarketIntelligence
from app.economics.models import ValuationFactors, ValuationResult
from app.models import DomainCandidate

VOWELS = set("aeiou")
BAD_PATTERNS = ("xq", "qz", "zx", "zz", "--")


class ValuationEngine:
    def __init__(
        self,
        settings: Settings,
        market: MarketIntelligence | None = None,
        sales: HistoricalSalesIntelligence | None = None,
        intelligence_client: DomainIntelligenceClient | None = None,
    ) -> None:
        self.settings = settings
        self.market = market or MarketIntelligence()
        self.sales = sales or HistoricalSalesIntelligence(self.market)
        self.intelligence_client = intelligence_client or DomainIntelligenceClient(settings)

    async def value(self, candidate: DomainCandidate, acquisition_cost: float = 12.0) -> ValuationResult:
        intelligence = await self.intelligence_client.enrich(candidate)
        stem, extension = self._parts(candidate.name)
        comparables = self.sales.find_comparables(candidate.name)
        comparable_anchor = self._comparable_anchor(candidate, intelligence)
        factors = self._factors(candidate, stem, extension, comparable_anchor, intelligence)
        weighted_score = self._weighted_score(factors)
        score = max(0, min(100, round(weighted_score * 100)))

        trend = self.market.trend_multiplier(candidate.name)
        fmv = max(50.0, comparable_anchor * (0.45 + weighted_score) * trend)
        resale_probability = max(0.01, min(0.95, factors.liquidity_probability * (0.55 + weighted_score / 2)))
        hold_days = max(14, round(420 * (1 - resale_probability) + 20 * (1 - factors.trend_momentum)))
        expected_sale_price = fmv * resale_probability
        expected_roi = (expected_sale_price - acquisition_cost) / max(acquisition_cost, 1)
        liquidity_adjusted_roi = expected_roi * resale_probability
        time_adjusted_roi = liquidity_adjusted_roi / max(1.0, hold_days / 30)
        confidence = max(0.0, min(1.0, (weighted_score * 0.65) + (resale_probability * 0.35)))
        list_price = self._list_price(fmv, resale_probability, hold_days)

        candidate.score = score
        return ValuationResult(
            domain=candidate.name,
            score=score,
            fair_market_value=round(fmv, 2),
            expected_resale_probability=round(resale_probability, 4),
            estimated_holding_days=hold_days,
            expected_sale_price=round(expected_sale_price, 2),
            expected_roi=round(expected_roi, 4),
            liquidity_adjusted_roi=round(liquidity_adjusted_roi, 4),
            time_adjusted_roi=round(time_adjusted_roi, 4),
            purchase_confidence=round(confidence, 4),
            recommended_purchase_price=round(min(fmv * 0.08, expected_sale_price * 0.25), 2),
            recommended_list_price=list_price,
            niche=self.market.niche_for_domain(candidate.name),
            extension=extension,
            factors=factors,
            comparable_count=len(comparables) + len(intelligence.namebio_sales),
            market_signals={
                "wayback_capture_count": intelligence.wayback.capture_count,
                "wayback_history_score": round(intelligence.wayback.history_score, 4),
                "namebio_comparable_median": round(intelligence.comparable_median, 2),
                "namebio_tld_average_price": round(intelligence.tld_average_price, 2),
                "namebio_keyword_average_price": round(intelligence.keyword_sales_average, 2),
                "backlink_count": candidate.backlinks,
                "age_years": candidate.age_years,
            },
        )

    def _comparable_anchor(self, candidate: DomainCandidate, intelligence: DomainIntelligence) -> float:
        anchors = [self.sales.median_price(candidate.name)]
        if intelligence.comparable_median > 0:
            anchors.append(intelligence.comparable_median)
        if intelligence.keyword_sales_average > 0:
            anchors.append(intelligence.keyword_sales_average)
        return sum(anchors) / len(anchors)

    def _factors(self, candidate: DomainCandidate, stem: str, extension: str, comparable_anchor: float, intelligence: DomainIntelligence) -> ValuationFactors:
        length = len(stem)
        extension_quality = self.settings.scoring.extension_points.get(extension, 0) / 10
        linguistic = self._linguistic_quality(stem)
        brandability = self._brandability(stem)
        commercial = self.market.commercial_intent(candidate.name)
        backlink_quality = min(1.0, math.log1p(max(0, candidate.backlinks)) / math.log(800))
        spam_safety = 0.25 if any(pattern in stem for pattern in BAD_PATTERNS) else 0.9
        trademark_safety = 0.25 if self._looks_like_trademark(stem) else 0.95
        archive_quality = max(min(1.0, max(0.1, candidate.age_years / 12)), intelligence.wayback.history_score)
        search_demand = min(1.0, (candidate.keyword_value + commercial * 10) / 25)
        cpc_value = min(1.0, commercial * 0.8 + (0.2 if "loan" in stem or "insurance" in stem else 0))
        liquidity = self._liquidity_probability(stem, extension, commercial, linguistic, brandability)
        trend = min(1.0, self.market.trend_multiplier(candidate.name) / 1.35)
        comparable_strength = min(1.0, comparable_anchor / 5000)
        return ValuationFactors(
            comparable_sales=comparable_strength,
            commercial_intent=commercial,
            cpc_value=cpc_value,
            search_demand=search_demand,
            extension_quality=extension_quality,
            linguistic_quality=linguistic,
            brandability=brandability,
            length_quality=max(0.0, 1 - abs(length - 8) / 16),
            pronounceability=self._pronounceability(stem),
            trend_momentum=trend,
            seo_authority=1.0 if candidate.google_indexed else 0.35,
            backlink_quality=backlink_quality,
            spam_safety=spam_safety,
            trademark_safety=trademark_safety,
            archive_quality=archive_quality,
            liquidity_probability=liquidity,
        )

    def _weighted_score(self, factors: ValuationFactors) -> float:
        weights = self.settings.valuation.weights
        values = factors.as_dict()
        total_weight = sum(weights.values()) or 1.0
        return sum(values.get(key, 0.0) * weight for key, weight in weights.items()) / total_weight

    def _list_price(self, fmv: float, resale_probability: float, hold_days: int) -> int:
        premium = 1.25 if resale_probability > 0.55 and hold_days < 180 else 1.05
        if hold_days > 270:
            premium = 0.85
        return int(round(max(99, fmv * premium) / 50) * 50)

    def _parts(self, domain: str) -> tuple[str, str]:
        stem, tld = domain.rsplit(".", 1)
        return stem.lower(), "." + tld.lower()

    def _linguistic_quality(self, stem: str) -> float:
        if not re.fullmatch(r"[a-z0-9-]+", stem):
            return 0.1
        penalty = sum(stem.count(pattern) for pattern in BAD_PATTERNS) * 0.25
        return max(0.0, min(1.0, self._pronounceability(stem) * 0.7 + (0.3 if "-" not in stem else 0.05) - penalty))

    def _brandability(self, stem: str) -> float:
        if any(char.isdigit() for char in stem):
            return 0.25
        return max(0.1, min(1.0, self._pronounceability(stem) * 0.6 + (0.4 if 5 <= len(stem) <= 11 else 0.1)))

    def _pronounceability(self, stem: str) -> float:
        letters = [char for char in stem if char.isalpha()]
        if not letters:
            return 0.0
        vowel_ratio = sum(char in VOWELS for char in letters) / len(letters)
        return max(0.1, min(1.0, 1 - abs(vowel_ratio - 0.42) * 2))

    def _liquidity_probability(self, stem: str, extension: str, commercial: float, linguistic: float, brandability: float) -> float:
        extension_base = {".com": 0.72, ".io": 0.48, ".net": 0.38, ".org": 0.36}.get(extension, 0.2)
        length_bonus = 0.15 if 4 <= len(stem) <= 10 else -0.12
        return max(0.02, min(0.95, extension_base + length_bonus + commercial * 0.18 + linguistic * 0.12 + brandability * 0.08))

    def _looks_like_trademark(self, stem: str) -> bool:
        risky_terms = {"google", "meta", "amazon", "tesla", "apple", "microsoft", "godaddy"}
        return any(term in stem for term in risky_terms)
