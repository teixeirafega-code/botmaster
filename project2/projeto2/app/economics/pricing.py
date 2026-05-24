from __future__ import annotations

from datetime import UTC, datetime

from app.economics.models import ValuationResult
from app.models import ManagedDomain


class DynamicPricingEngine:
    def price_for_acquisition(self, valuation: ValuationResult) -> int:
        return valuation.recommended_list_price

    def repricing_recommendation(self, domain: ManagedDomain, valuation: ValuationResult) -> int:
        if not domain.registered_at:
            return valuation.recommended_list_price
        days_held = (datetime.now(UTC) - domain.registered_at).days
        price = float(valuation.recommended_list_price)
        if days_held > 365:
            price *= 0.65
        elif days_held > 180:
            price *= 0.8
        elif days_held > 90:
            price *= 0.9
        return int(round(max(99, price) / 50) * 50)
