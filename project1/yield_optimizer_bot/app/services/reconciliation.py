from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.portfolio_manager import PortfolioManager
from app.services.position_indexer import PortfolioSnapshot, PositionIndexer


@dataclass(frozen=True)
class ReconciliationResult:
    snapshot: PortfolioSnapshot
    cache_updated: bool
    inconsistencies: list[str] = field(default_factory=list)


class ReconciliationService:
    def __init__(self, position_indexer: PositionIndexer, portfolio_manager: PortfolioManager):
        self.position_indexer = position_indexer
        self.portfolio_manager = portfolio_manager

    async def reconcile(self, asset_symbols: list[str]) -> ReconciliationResult:
        snapshot = await self.position_indexer.snapshot(asset_symbols)
        previous_holdings = dict(self.portfolio_manager.state.holdings)
        inconsistencies: list[str] = []

        for asset_symbol, total_balance in snapshot.totals_by_asset.items():
            cached_balance = int(previous_holdings.get(asset_symbol, 0))
            if cached_balance != total_balance:
                inconsistencies.append(
                    f"holding_mismatch:{asset_symbol}:cached={cached_balance}:onchain={total_balance}"
                )

        self.portfolio_manager.update_from_snapshot(snapshot)
        return ReconciliationResult(
            snapshot=snapshot,
            cache_updated=True,
            inconsistencies=inconsistencies,
        )
