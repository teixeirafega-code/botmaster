from __future__ import annotations

from collections import defaultdict

from app.models import ManagedDomain


class ProfitabilityAnalytics:
    def roi_by_extension(self, domains: list[ManagedDomain]) -> dict[str, float]:
        grouped: dict[str, list[ManagedDomain]] = defaultdict(list)
        for domain in domains:
            grouped["." + domain.name.rsplit(".", 1)[-1].lower()].append(domain)
        return {extension: self._roi(items) for extension, items in grouped.items()}

    def roi_by_score_range(self, domains: list[ManagedDomain]) -> dict[str, float]:
        ranges: dict[str, list[ManagedDomain]] = {"60-69": [], "70-79": [], "80-89": [], "90-100": []}
        for domain in domains:
            if domain.score >= 90:
                ranges["90-100"].append(domain)
            elif domain.score >= 80:
                ranges["80-89"].append(domain)
            elif domain.score >= 70:
                ranges["70-79"].append(domain)
            else:
                ranges["60-69"].append(domain)
        return {key: self._roi(value) for key, value in ranges.items() if value}

    def _roi(self, domains: list[ManagedDomain]) -> float:
        cost = sum(domain.acquisition_cost for domain in domains)
        sales = sum(domain.sale_price for domain in domains)
        return round((sales - cost) / max(cost, 1), 4)
