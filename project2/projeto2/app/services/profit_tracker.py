from __future__ import annotations

from app.models import DomainStatus, ManagedDomain


class ProfitTracker:
    def snapshot(self, domains: list[ManagedDomain]) -> dict[str, float | int]:
        invested = sum(domain.acquisition_cost for domain in domains)
        sales = sum(domain.sale_price for domain in domains if domain.status == DomainStatus.SOLD)
        return {
            "domains_monitored": len(domains),
            "registered": sum(domain.status in {DomainStatus.REGISTERED, DomainStatus.LISTED, DomainStatus.SOLD} for domain in domains),
            "sold": sum(domain.status == DomainStatus.SOLD for domain in domains),
            "total_invested": round(invested, 2),
            "total_profit": round(sales - invested, 2),
        }

