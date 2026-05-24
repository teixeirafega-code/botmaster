from __future__ import annotations

from datetime import UTC, datetime

from app.models import ManagedDomain


class PortfolioManager:
    def stale_inventory(self, domains: list[ManagedDomain], stale_days: int = 180) -> list[ManagedDomain]:
        now = datetime.now(UTC)
        return [
            domain
            for domain in domains
            if domain.registered_at and domain.sale_price <= 0 and (now - domain.registered_at).days >= stale_days
        ]

    def portfolio_quality_score(self, domains: list[ManagedDomain]) -> float:
        if not domains:
            return 0.0
        return round(sum(domain.score for domain in domains) / (len(domains) * 100), 4)

