from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.protocols.base_protocol import PositionSnapshot
from app.services.portfolio_manager import PortfolioManager
from app.services.reconciliation import ReconciliationService


class FakeIndexer:
    async def snapshot(self, asset_symbols: list[str]):
        asset = asset_symbols[0]
        snapshot = PositionSnapshot(
            protocol="aave",
            chain="Polygon",
            asset_symbol=asset,
            wallet_balance_wei=100,
            supplied_balance_wei=400,
            withdrawable_balance_wei=400,
            allowance_wei=500,
            shares_balance_wei=400,
            details={},
        )
        return SimpleNamespace(
            positions={"aave": {asset: snapshot}},
            totals_by_asset={asset: 500},
            dominant_protocol_by_asset={asset: "aave"},
        )


@pytest.mark.asyncio
async def test_reconciliation_updates_cache_from_onchain_snapshot(tmp_path):
    portfolio = PortfolioManager(str(tmp_path / "state.json"))
    service = ReconciliationService(position_indexer=FakeIndexer(), portfolio_manager=portfolio)

    result = await service.reconcile(["USDC"])

    assert result.cache_updated is True
    assert portfolio.state.holdings["USDC"] == 500
    assert portfolio.state.current_protocol == "aave"
