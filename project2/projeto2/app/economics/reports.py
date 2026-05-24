from __future__ import annotations

from app.economics.analytics import ProfitabilityAnalytics
from app.economics.portfolio import PortfolioManager
from app.models import ManagedDomain


class EconomicReportBuilder:
    def daily_report(self, domains: list[ManagedDomain]) -> str:
        portfolio = PortfolioManager()
        analytics = ProfitabilityAnalytics()
        stale = portfolio.stale_inventory(domains)
        quality = portfolio.portfolio_quality_score(domains)
        by_extension = analytics.roi_by_extension(domains)
        return (
            "Daily economic report\n"
            f"Inventory: {len(domains)}\n"
            f"Portfolio quality: {quality}\n"
            f"Stale inventory: {len(stale)}\n"
            f"ROI by extension: {by_extension}"
        )

