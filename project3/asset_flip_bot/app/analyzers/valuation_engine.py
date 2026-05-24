from __future__ import annotations

from app.analyzers.revenue_multiplier import RevenueMultiplier
from app.models import MarketplaceListing, Valuation


class ValuationEngine:
    def __init__(self, multiplier: RevenueMultiplier) -> None:
        self.multiplier = multiplier

    def estimate(self, listing: MarketplaceListing) -> Valuation:
        monthly_cashflow = max(listing.monthly_profit, listing.monthly_revenue, 0.0)
        low_multiplier, high_multiplier = self.multiplier.for_listing(listing)
        low_value = monthly_cashflow * low_multiplier
        high_value = monthly_cashflow * high_multiplier
        estimated = (low_value + high_value) / 2
        asking = max(listing.asking_price, 0.0)
        profit_potential = max(estimated - asking, 0.0)
        discount = 0.0
        if estimated > 0 and asking > 0:
            discount = asking / estimated

        return Valuation(
            low_value=round(low_value, 2),
            high_value=round(high_value, 2),
            estimated_real_value=round(estimated, 2),
            profit_potential=round(profit_potential, 2),
            discount_to_value=round(discount, 4),
            multiplier_low=low_multiplier,
            multiplier_high=high_multiplier,
        )

