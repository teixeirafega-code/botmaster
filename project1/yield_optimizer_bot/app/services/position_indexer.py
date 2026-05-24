from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.protocols.base_protocol import BaseProtocol, PositionSnapshot


@dataclass(frozen=True)
class PortfolioSnapshot:
    wallet_address: str
    created_at: float
    positions: dict[str, dict[str, PositionSnapshot]]
    totals_by_asset: dict[str, int]
    dominant_protocol_by_asset: dict[str, str | None]
    details: dict[str, Any] = field(default_factory=dict)


class PositionIndexer:
    def __init__(self, protocols: list[BaseProtocol], wallet_address: str):
        self.protocols = protocols
        self.wallet_address = wallet_address

    async def snapshot(self, asset_symbols: list[str]) -> PortfolioSnapshot:
        tasks = []
        task_meta: list[tuple[str, str]] = []
        for protocol in self.protocols:
            for asset_symbol in asset_symbols:
                if asset_symbol not in protocol.supported_assets():
                    continue
                tasks.append(protocol.discover_position(self.wallet_address, asset_symbol))
                task_meta.append((protocol.protocol_name, asset_symbol))

        results = await asyncio.gather(*tasks)

        positions: dict[str, dict[str, PositionSnapshot]] = {}
        totals_by_asset: dict[str, int] = {asset_symbol: 0 for asset_symbol in asset_symbols}
        wallet_balance_by_asset: dict[str, int | None] = {asset_symbol: None for asset_symbol in asset_symbols}
        dominant_protocol_by_asset: dict[str, str | None] = {asset_symbol: None for asset_symbol in asset_symbols}
        dominant_balance_by_asset: dict[str, int] = {asset_symbol: -1 for asset_symbol in asset_symbols}

        for snapshot in results:
            positions.setdefault(snapshot.protocol, {})[snapshot.asset_symbol] = snapshot
            if wallet_balance_by_asset[snapshot.asset_symbol] is None:
                wallet_balance_by_asset[snapshot.asset_symbol] = snapshot.wallet_balance_wei
                totals_by_asset[snapshot.asset_symbol] += snapshot.wallet_balance_wei
            totals_by_asset[snapshot.asset_symbol] += snapshot.supplied_balance_wei
            if snapshot.supplied_balance_wei > dominant_balance_by_asset[snapshot.asset_symbol]:
                dominant_balance_by_asset[snapshot.asset_symbol] = snapshot.supplied_balance_wei
                dominant_protocol_by_asset[snapshot.asset_symbol] = snapshot.protocol if snapshot.supplied_balance_wei > 0 else None

        return PortfolioSnapshot(
            wallet_address=self.wallet_address,
            created_at=time.time(),
            positions=positions,
            totals_by_asset=totals_by_asset,
            dominant_protocol_by_asset=dominant_protocol_by_asset,
        )
