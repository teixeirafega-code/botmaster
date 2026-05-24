from __future__ import annotations

import pytest

from app.protocols.base_protocol import BaseProtocol, PositionSnapshot, YieldQuote
from app.services.position_indexer import PositionIndexer


class FakePositionProtocol(BaseProtocol):
    def __init__(self, chain: str, protocol_name: str, wallet_balance_wei: int, supplied_balance_wei: int):
        super().__init__(chain=chain)
        self.protocol_name = protocol_name
        self.wallet_balance_wei = wallet_balance_wei
        self.supplied_balance_wei = supplied_balance_wei

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        raise NotImplementedError

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict:
        raise NotImplementedError

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict:
        raise NotImplementedError

    def supported_assets(self) -> list[str]:
        return ["USDC"]

    def get_spender(self, asset_symbol: str) -> str:
        return "0x0000000000000000000000000000000000000001"

    def get_asset_address(self, asset_symbol: str) -> str:
        return "0x0000000000000000000000000000000000000002"

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        return PositionSnapshot(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol,
            wallet_balance_wei=self.wallet_balance_wei,
            supplied_balance_wei=self.supplied_balance_wei,
            withdrawable_balance_wei=self.supplied_balance_wei,
            allowance_wei=0,
            shares_balance_wei=self.supplied_balance_wei,
        )


@pytest.mark.asyncio
async def test_position_indexer_counts_wallet_once_and_sums_supply():
    protocols = [
        FakePositionProtocol(chain="Polygon", protocol_name="aave", wallet_balance_wei=100, supplied_balance_wei=400),
        FakePositionProtocol(chain="Polygon", protocol_name="compound", wallet_balance_wei=100, supplied_balance_wei=200),
    ]
    indexer = PositionIndexer(protocols=protocols, wallet_address="0x1111111111111111111111111111111111111111")

    snapshot = await indexer.snapshot(["USDC"])

    assert snapshot.totals_by_asset["USDC"] == 700
    assert snapshot.dominant_protocol_by_asset["USDC"] == "aave"
