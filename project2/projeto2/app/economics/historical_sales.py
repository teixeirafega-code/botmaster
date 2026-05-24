from __future__ import annotations

from datetime import UTC, datetime
from statistics import median

from app.economics.market_intelligence import MarketIntelligence
from app.economics.models import ComparableSale

SEED_COMPARABLES = [
    ComparableSale("aicloud.com", 4200, datetime(2024, 1, 10, tzinfo=UTC), niche="ai", extension=".com"),
    ComparableSale("datahub.com", 7800, datetime(2023, 9, 18, tzinfo=UTC), niche="data", extension=".com"),
    ComparableSale("healthwise.org", 1900, datetime(2023, 5, 4, tzinfo=UTC), niche="health", extension=".org"),
    ComparableSale("financely.net", 1200, datetime(2022, 11, 20, tzinfo=UTC), niche="finance", extension=".net"),
    ComparableSale("cloudpilot.io", 2600, datetime(2024, 4, 2, tzinfo=UTC), niche="cloud", extension=".io"),
]


class HistoricalSalesIntelligence:
    def __init__(self, market: MarketIntelligence | None = None, comparables: list[ComparableSale] | None = None) -> None:
        self.market = market or MarketIntelligence()
        self.comparables = comparables or SEED_COMPARABLES

    def find_comparables(self, domain: str) -> list[ComparableSale]:
        niche = self.market.niche_for_domain(domain)
        extension = "." + domain.rsplit(".", 1)[-1].lower()
        matches = [sale for sale in self.comparables if sale.niche == niche or sale.extension == extension]
        return sorted(matches, key=lambda sale: (sale.niche == niche, sale.extension == extension), reverse=True)[:10]

    def median_price(self, domain: str) -> float:
        matches = self.find_comparables(domain)
        if not matches:
            return 350.0
        return float(median(sale.sale_price for sale in matches))

    def extension_performance(self, extension: str) -> float:
        prices = [sale.sale_price for sale in self.comparables if sale.extension == extension]
        if not prices:
            return 1.0
        all_median = median(sale.sale_price for sale in self.comparables)
        return max(0.5, min(2.0, median(prices) / all_median))

