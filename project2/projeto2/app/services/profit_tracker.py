from __future__ import annotations

from datetime import UTC, datetime

from app.models import DomainStatus, ManagedDomain


class ProfitTracker:
    def snapshot(self, domains: list[ManagedDomain]) -> dict[str, float | int]:
        invested = sum(domain.acquisition_cost for domain in domains)
        sales = sum(domain.sale_price for domain in domains if domain.status == DomainStatus.SOLD)
        portfolio_value = sum(domain.sale_price if domain.status == DomainStatus.SOLD else domain.asking_price for domain in domains)
        return {
            "domains_monitored": len(domains),
            "registered": sum(domain.status in {DomainStatus.REGISTERED, DomainStatus.LISTED, DomainStatus.SOLD} for domain in domains),
            "sold": sum(domain.status == DomainStatus.SOLD for domain in domains),
            "total_invested": round(invested, 2),
            "total_profit": round(sales - invested, 2),
            "total_portfolio_value": round(portfolio_value, 2),
            "unrealized_value": round(portfolio_value - sales, 2),
        }

    def domain_rows(self, domains: list[ManagedDomain]) -> list[dict[str, float | int | str]]:
        rows: list[dict[str, float | int | str]] = []
        for domain in domains:
            revenue = domain.sale_price if domain.status == DomainStatus.SOLD else 0.0
            roi = (revenue - domain.acquisition_cost) / max(domain.acquisition_cost, 1.0) if revenue else 0.0
            days_listed = 0
            if domain.listed_at:
                days_listed = max(0, (datetime.now(UTC) - domain.listed_at).days)
            rows.append(
                {
                    "domain": domain.name,
                    "status": domain.status.value,
                    "cost": round(domain.acquisition_cost, 2),
                    "list_price": domain.asking_price,
                    "sale_price": round(domain.sale_price, 2),
                    "days_listed": days_listed,
                    "roi": round(roi, 4),
                    "marketplaces": ",".join(domain.marketplaces),
                }
            )
        return rows

    def sold_domains(self, previous: list[ManagedDomain], current: list[ManagedDomain]) -> list[ManagedDomain]:
        previous_status = {domain.name: domain.status for domain in previous}
        return [domain for domain in current if domain.status == DomainStatus.SOLD and previous_status.get(domain.name) != DomainStatus.SOLD]
