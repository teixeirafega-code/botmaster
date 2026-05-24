from __future__ import annotations

from app.models import MarketplaceListing, ScoredOpportunity, Valuation


class OpportunityScorer:
    def __init__(
        self,
        undervalued_threshold: float = 0.5,
        niche_bonus: dict[str, int] | None = None,
    ) -> None:
        self.undervalued_threshold = undervalued_threshold
        self.niche_bonus = niche_bonus or {}

    def score(self, listing: MarketplaceListing, valuation: Valuation) -> ScoredOpportunity:
        score = 0.0
        reasons: list[str] = []

        if valuation.estimated_real_value > 0 and listing.asking_price > 0:
            discount = 1 - valuation.discount_to_value
            discount_score = max(0.0, min(35.0, discount * 70.0))
            score += discount_score
            if valuation.discount_to_value < self.undervalued_threshold:
                reasons.append(
                    f"Asking price is {valuation.discount_to_value:.0%} of estimated value"
                )

        monthly_cashflow = max(listing.monthly_profit, listing.monthly_revenue)
        if monthly_cashflow >= 20_000:
            score += 25
            reasons.append("Strong monthly cashflow")
        elif monthly_cashflow >= 10_000:
            score += 21
        elif monthly_cashflow >= 5_000:
            score += 17
        elif monthly_cashflow >= 1_000:
            score += 12
        elif monthly_cashflow > 0:
            score += 7

        if listing.age_months >= 36:
            score += 15
            reasons.append("Mature operating history")
        elif listing.age_months >= 24:
            score += 12
        elif listing.age_months >= 12:
            score += 8
        elif listing.age_months > 0:
            score += 4

        if listing.monthly_traffic >= 500_000:
            score += 15
            reasons.append("High monthly traffic")
        elif listing.monthly_traffic >= 100_000:
            score += 12
        elif listing.monthly_traffic >= 25_000:
            score += 8
        elif listing.monthly_traffic > 0:
            score += 4

        niche = listing.niche.lower()
        for key, bonus in self.niche_bonus.items():
            if key in niche:
                score += bonus
                reasons.append(f"Attractive niche: {listing.niche}")
                break

        if valuation.profit_potential > 0:
            reasons.append(f"Estimated upside ${valuation.profit_potential:,.0f}")

        is_undervalued = (
            valuation.estimated_real_value > 0
            and listing.asking_price > 0
            and valuation.discount_to_value < self.undervalued_threshold
        )

        return ScoredOpportunity(
            listing=listing,
            valuation=valuation,
            opportunity_score=int(max(0, min(100, round(score)))),
            is_undervalued=is_undervalued,
            reasons=reasons,
        )

