from __future__ import annotations

from collections import Counter

from app.config.settings import Settings
from app.economics.models import ValuationResult
from app.models import ManagedDomain


class CapitalAllocator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def allowed(self, valuation: ValuationResult, portfolio: list[ManagedDomain]) -> tuple[bool, str]:
        exposure = sum(domain.acquisition_cost for domain in portfolio)
        if exposure + valuation.recommended_purchase_price > self.settings.economics.max_portfolio_capital:
            return False, "portfolio_capital_limit"

        extensions = Counter("." + domain.name.rsplit(".", 1)[-1].lower() for domain in portfolio)
        if extensions and extensions[valuation.extension] / max(1, len(portfolio)) > self.settings.economics.max_extension_concentration:
            return False, "extension_concentration_limit"

        niche_count = sum(1 for domain in portfolio if valuation.niche in domain.name.lower())
        if portfolio and niche_count / len(portfolio) > self.settings.economics.max_niche_concentration:
            return False, "niche_concentration_limit"

        return True, "capital_available"

