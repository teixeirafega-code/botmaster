from __future__ import annotations

import pytest

from app.protocols.base_protocol import BaseProtocol, YieldQuote
from app.services.apy_aggregator import APYAggAggregator, APYAggConfig


class FakeProtocol(BaseProtocol):
    protocol_name = "fake"

    def __init__(self, chain: str, raw_apy: float):
        super().__init__(chain=chain)
        self._raw_apy = raw_apy
        self.calls = 0

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        self.calls += 1
        return YieldQuote(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol,
            raw_apy=self._raw_apy,
            net_apy=self._raw_apy,
            details={"asset_symbol": asset_symbol},
        )

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict:
        raise NotImplementedError

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict:
        raise NotImplementedError

    def supported_assets(self) -> list[str]:
        return ["USDC", "USDT"]

    def get_spender(self, asset_symbol: str) -> str:
        return "0x0000000000000000000000000000000000000001"

    def get_asset_address(self, asset_symbol: str) -> str:
        return "0x0000000000000000000000000000000000000002"


@pytest.mark.asyncio
async def test_apy_aggregator_aggregates_protocols():
    p1 = FakeProtocol(chain="Ethereum", raw_apy=0.10)
    p2 = FakeProtocol(chain="Ethereum", raw_apy=0.20)

    agg = APYAggAggregator(
        protocols=[p1, p2],
        cfg=APYAggConfig(cache_ttl_seconds=0, timeout_seconds=1),
    )

    quotes = await agg.aggregate(asset_symbol="USDC")
    assert len(quotes) == 2
    assert all(q.asset_symbol == "USDC" for q in quotes)
    assert {q.protocol for q in quotes} == {"fake"}


@pytest.mark.asyncio
async def test_apy_aggregator_cache_hit_within_ttl():
    p = FakeProtocol(chain="Ethereum", raw_apy=0.15)
    agg = APYAggAggregator(
        protocols=[p],
        cfg=APYAggConfig(cache_ttl_seconds=60, timeout_seconds=1),
    )

    _ = await agg.aggregate(asset_symbol="USDT")
    _ = await agg.aggregate(asset_symbol="USDT")

    assert p.calls == 1
