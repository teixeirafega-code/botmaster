from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class YieldQuote:
    protocol: str
    chain: str
    asset_symbol: str
    net_apy: float
    raw_apy: float
    details: dict[str, Any]


@dataclass(frozen=True)
class ProtocolHealth:
    protocol: str
    asset_symbol: str
    is_healthy: bool
    is_paused: bool
    liquidity_wei: int
    risk_score: float
    confidence_score: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreviewResult:
    asset_symbol: str
    amount_wei: int
    expected_shares_wei: int
    expected_assets_wei: int
    price_impact_bps: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionSnapshot:
    protocol: str
    chain: str
    asset_symbol: str
    wallet_balance_wei: int
    supplied_balance_wei: int
    withdrawable_balance_wei: int
    allowance_wei: int
    shares_balance_wei: int
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def total_balance_wei(self) -> int:
        return int(self.wallet_balance_wei) + int(self.supplied_balance_wei)


class BaseProtocol(abc.ABC):
    protocol_name: str

    def __init__(self, chain: str):
        self.chain = chain

    @abc.abstractmethod
    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        raise NotImplementedError

    @abc.abstractmethod
    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def supported_assets(self) -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    def get_spender(self, asset_symbol: str) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def get_asset_address(self, asset_symbol: str) -> str:
        raise NotImplementedError

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        raise NotImplementedError(f"{self.protocol_name} does not implement position discovery")

    async def preview_deposit(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return PreviewResult(
            asset_symbol=asset_symbol,
            amount_wei=amount_wei,
            expected_shares_wei=amount_wei,
            expected_assets_wei=amount_wei,
            price_impact_bps=0,
        )

    async def preview_withdraw(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return PreviewResult(
            asset_symbol=asset_symbol,
            amount_wei=amount_wei,
            expected_shares_wei=amount_wei,
            expected_assets_wei=amount_wei,
            price_impact_bps=0,
        )

    async def check_health(self, asset_symbol: str) -> ProtocolHealth:
        return ProtocolHealth(
            protocol=self.protocol_name,
            asset_symbol=asset_symbol,
            is_healthy=True,
            is_paused=False,
            liquidity_wei=0,
            risk_score=0.5,
            confidence_score=0.5,
        )
