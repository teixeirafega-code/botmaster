from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import ScoredOpportunity


class ProfitTracker:
    def __init__(self, stats_path: str | Path, max_history: int = 500) -> None:
        self.stats_path = Path(stats_path)
        self.max_history = max_history
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.stats_path.exists():
            return self._empty()
        try:
            with self.stats_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            return self._empty()

    def record_scan(
        self,
        assets_monitored: int,
        opportunities: list[ScoredOpportunity],
    ) -> dict[str, Any]:
        stats = self.load()
        existing = stats.get("opportunities", [])
        seen = {item.get("alert_key") for item in existing}
        for opportunity in opportunities:
            alert_key = self.alert_key(opportunity)
            if alert_key in seen:
                continue
            item = opportunity.to_dict()
            item["alert_key"] = alert_key
            existing.append(item)
            seen.add(alert_key)

        existing = sorted(
            existing,
            key=lambda item: item.get("detected_at", ""),
            reverse=True,
        )[: self.max_history]
        total_profit = sum(
            float(item.get("valuation", {}).get("profit_potential", 0.0))
            for item in existing
        )
        stats = {
            "assets_monitored": assets_monitored,
            "opportunities_found": len(existing),
            "total_potential_profit": round(total_profit, 2),
            "last_scan_at": datetime.now(timezone.utc).isoformat(),
            "opportunities": existing,
        }
        self._write(stats)
        return stats

    def alert_key(self, opportunity: ScoredOpportunity) -> str:
        listing = opportunity.listing
        valuation = opportunity.valuation
        return (
            f"{listing.stable_key}:"
            f"{int(listing.asking_price)}:"
            f"{int(valuation.estimated_real_value)}:"
            f"{opportunity.opportunity_score}"
        )

    def _empty(self) -> dict[str, Any]:
        return {
            "assets_monitored": 0,
            "opportunities_found": 0,
            "total_potential_profit": 0.0,
            "last_scan_at": None,
            "opportunities": [],
        }

    def _write(self, data: dict[str, Any]) -> None:
        temp_path = self.stats_path.with_suffix(self.stats_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        temp_path.replace(self.stats_path)

