from __future__ import annotations

import asyncio
import logging
from typing import Any

from web3 import Web3

from app.utils.retry import RetryConfig, retry_async

from .base_protocol import BaseProtocol, PositionSnapshot, PreviewResult, ProtocolHealth, YieldQuote

SECONDS_PER_YEAR = 60 * 60 * 24 * 365

ERC20_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}, {"internalType": "address", "name": "spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class CompoundProtocol(BaseProtocol):
    protocol_name = "compound"

    _MARKETS: dict[str, dict[str, Any]] = {
        "USDC": {
            "asset_address": Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
            "comet_address": Web3.to_checksum_address("0xF25212E676D1F7F89Cd72fFEe66158f541246445"),
            "decimals": 6,
        },
        "USDT": {
            "asset_address": Web3.to_checksum_address("0xc2132D05D31c914a87C6611C10748AEb04B58e8F"),
            "comet_address": Web3.to_checksum_address("0xaeB318360f27748Acb200CE616E389A6C9409a07"),
            "decimals": 6,
        },
    }

    _COMET_ABI: list[dict[str, Any]] = [
        {
            "inputs": [],
            "name": "getUtilization",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "uint256", "name": "utilization", "type": "uint256"}],
            "name": "getSupplyRate",
            "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [
                {"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
            ],
            "name": "supply",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [
                {"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
            ],
            "name": "withdraw",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]

    def __init__(self, chain: str, w3: Web3, wallet_address: str | None = None):
        super().__init__(chain=chain)
        self.w3 = w3
        self.wallet_address = Web3.to_checksum_address(wallet_address) if wallet_address else None
        self.logger = logging.getLogger("yield-optimizer-bot.protocols.compound")
        self._contracts = {
            symbol: self.w3.eth.contract(address=cfg["comet_address"], abi=self._COMET_ABI)
            for symbol, cfg in self._MARKETS.items()
        }
        self._token_contracts = {
            symbol: self.w3.eth.contract(address=cfg["asset_address"], abi=ERC20_ABI)
            for symbol, cfg in self._MARKETS.items()
        }

    def _market(self, asset_symbol: str) -> dict[str, Any]:
        try:
            return self._MARKETS[asset_symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Asset not supported by Compound on Polygon: {asset_symbol}") from exc

    def _contract(self, asset_symbol: str):
        return self._contracts[asset_symbol.upper()]

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError("wallet_address is required to build Compound transaction plans")
        return self.wallet_address

    def supported_assets(self) -> list[str]:
        return list(self._MARKETS.keys())

    def get_spender(self, asset_symbol: str) -> str:
        return self._market(asset_symbol)["comet_address"]

    def get_asset_address(self, asset_symbol: str) -> str:
        return self._market(asset_symbol)["asset_address"]

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        market = self._market(asset_symbol)
        contract = self._contract(asset_symbol)

        async def call_market_data():
            utilization = await asyncio.to_thread(contract.functions.getUtilization().call)
            supply_rate = await asyncio.to_thread(contract.functions.getSupplyRate(int(utilization)).call)
            return int(utilization), int(supply_rate)

        utilization, supply_rate = await retry_async(
            call_market_data,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

        raw_apy = (supply_rate / 10**18) * SECONDS_PER_YEAR

        self.logger.info(
            "Compound APY fetched | asset=%s comet=%s utilization=%s supply_rate=%s raw_apy=%.8f",
            asset_symbol.upper(),
            market["comet_address"],
            utilization,
            supply_rate,
            raw_apy,
        )

        return YieldQuote(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            net_apy=raw_apy,
            raw_apy=raw_apy,
            details={
                "comet_address": market["comet_address"],
                "asset_address": market["asset_address"],
                "utilization": utilization,
                "supply_rate_per_second_scaled_1e18": supply_rate,
            },
        )

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        market = self._market(asset_symbol)
        token = self._token_contracts[asset_symbol.upper()]
        contract = self._contract(asset_symbol)
        user = Web3.to_checksum_address(wallet_address)

        async def fn():
            wallet_balance = await asyncio.to_thread(token.functions.balanceOf(user).call)
            allowance = await asyncio.to_thread(token.functions.allowance(user, market["comet_address"]).call)
            supplied_balance = await asyncio.to_thread(contract.functions.balanceOf(user).call)
            return int(wallet_balance), int(allowance), int(supplied_balance)

        wallet_balance, allowance, supplied_balance = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

        return PositionSnapshot(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            wallet_balance_wei=wallet_balance,
            supplied_balance_wei=supplied_balance,
            withdrawable_balance_wei=supplied_balance,
            allowance_wei=allowance,
            shares_balance_wei=supplied_balance,
            details={"comet_address": market["comet_address"], "asset_address": market["asset_address"]},
        )

    async def preview_deposit(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        self._market(asset_symbol)
        return PreviewResult(
            asset_symbol=asset_symbol.upper(),
            amount_wei=int(amount_wei),
            expected_shares_wei=int(amount_wei),
            expected_assets_wei=int(amount_wei),
            price_impact_bps=0,
            details={"preview_model": "compound_base_supply"},
        )

    async def preview_withdraw(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return await self.preview_deposit(asset_symbol, amount_wei)

    async def check_health(self, asset_symbol: str) -> ProtocolHealth:
        market = self._market(asset_symbol)
        contract = self._contract(asset_symbol)

        async def fn():
            utilization = await asyncio.to_thread(contract.functions.getUtilization().call)
            supply_rate = await asyncio.to_thread(contract.functions.getSupplyRate(int(utilization)).call)
            return int(utilization), int(supply_rate)

        utilization, supply_rate = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        healthy = supply_rate >= 0 and utilization >= 0
        return ProtocolHealth(
            protocol=self.protocol_name,
            asset_symbol=asset_symbol.upper(),
            is_healthy=healthy,
            is_paused=False,
            liquidity_wei=0,
            risk_score=0.18,
            confidence_score=0.88,
            details={
                "comet_address": market["comet_address"],
                "utilization": utilization,
                "supply_rate_per_second_scaled_1e18": supply_rate,
            },
        )

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        market = self._market(asset_symbol)
        sender = self._require_wallet_address()
        tx_data = self._contract(asset_symbol).functions.withdraw(
            market["asset_address"],
            int(amount_wei),
        )._encode_transaction_data()

        return {
            "to": market["comet_address"],
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 450_000,
        }

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        market = self._market(asset_symbol)
        sender = self._require_wallet_address()
        tx_data = self._contract(asset_symbol).functions.supply(
            market["asset_address"],
            int(amount_wei),
        )._encode_transaction_data()

        return {
            "to": market["comet_address"],
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 450_000,
        }
