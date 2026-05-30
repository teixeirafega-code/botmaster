from __future__ import annotations

from datetime import UTC, datetime

from app.config.settings import PricingSettings
from app.economics.models import ValuationResult
from app.models import DomainCandidate, ManagedDomain


class DynamicPricingEngine:
    def __init__(self, settings: PricingSettings | None = None) -> None:
        self.settings = settings or PricingSettings()

    def price_for_acquisition(self, valuation: ValuationResult) -> int:
        return valuation.recommended_list_price

    def smart_price(self, candidate: DomainCandidate, valuation: ValuationResult) -> int:
        keyword_boost = 1 + min(0.35, candidate.keyword_value / 100)
        backlink_boost = 1 + min(0.3, candidate.backlinks / 3000)
        age_boost = 1 + min(0.25, candidate.age_years / 80)
        comparable = float(valuation.market_signals.get("namebio_comparable_median", 0) or 0)
        base = max(float(valuation.recommended_list_price), comparable * 0.85, float(self.settings.minimum_list_price))
        price = base * keyword_boost * backlink_boost * age_boost
        if valuation.expected_resale_probability < 0.25:
            price *= 0.85
        return int(round(max(self.settings.minimum_list_price, price) / 50) * 50)

    def repricing_recommendation(self, domain: ManagedDomain, valuation: ValuationResult) -> int:
        listed_at = domain.listed_at or domain.registered_at
        if not listed_at:
            return valuation.recommended_list_price
        days_held = (datetime.now(UTC) - listed_at).days
        price = float(domain.asking_price or valuation.recommended_list_price)
        if self.settings.reprice_after_days <= days_held <= 90:
            price *= self.settings.stale_discount_rate
        if days_held > 365:
            price *= 0.65
        elif days_held > 180:
            price *= 0.8
        elif days_held > 90:
            price *= 0.9
        return int(round(max(self.settings.minimum_list_price, price) / 50) * 50)
