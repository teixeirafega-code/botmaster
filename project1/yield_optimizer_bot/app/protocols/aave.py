from __future__ import annotations

import asyncio
import logging
from typing import Any

from web3 import Web3

from app.utils.retry import RetryConfig, retry_async

from .base_protocol import BaseProtocol, PositionSnapshot, PreviewResult, ProtocolHealth, YieldQuote

RAY = 10**27
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


class AaveProtocol(BaseProtocol):
    protocol_name = "aave"

    _POOL_ADDRESS = Web3.to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD")
    _DATA_PROVIDER_ADDRESS = Web3.to_checksum_address("0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654")

    _ASSETS: dict[str, dict[str, Any]] = {
        "USDC": {
            "address": Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
            "decimals": 6,
        },
        "USDT": {
            "address": Web3.to_checksum_address("0xc2132D05D31c914a87C6611C10748AEb04B58e8F"),
            "decimals": 6,
        },
    }

    _DATA_PROVIDER_ABI: list[dict[str, Any]] = [
        {
            "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
            "name": "getReserveData",
            "outputs": [
                {"internalType": "uint256", "name": "unbacked", "type": "uint256"},
                {"internalType": "uint256", "name": "accruedToTreasuryScaled", "type": "uint256"},
                {"internalType": "uint256", "name": "totalAToken", "type": "uint256"},
                {"internalType": "uint256", "name": "totalStableDebt", "type": "uint256"},
                {"internalType": "uint256", "name": "totalVariableDebt", "type": "uint256"},
                {"internalType": "uint256", "name": "liquidityRate", "type": "uint256"},
                {"internalType": "uint256", "name": "variableBorrowRate", "type": "uint256"},
                {"internalType": "uint256", "name": "stableBorrowRate", "type": "uint256"},
                {"internalType": "uint256", "name": "averageStableBorrowRate", "type": "uint256"},
                {"internalType": "uint256", "name": "liquidityIndex", "type": "uint256"},
                {"internalType": "uint256", "name": "variableBorrowIndex", "type": "uint256"},
                {"internalType": "uint40", "name": "lastUpdateTimestamp", "type": "uint40"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "address", "name": "asset", "type": "address"}, {"internalType": "address", "name": "user", "type": "address"}],
            "name": "getUserReserveData",
            "outputs": [
                {"internalType": "uint256", "name": "currentATokenBalance", "type": "uint256"},
                {"internalType": "uint256", "name": "currentStableDebt", "type": "uint256"},
                {"internalType": "uint256", "name": "currentVariableDebt", "type": "uint256"},
                {"internalType": "uint256", "name": "principalStableDebt", "type": "uint256"},
                {"internalType": "uint256", "name": "scaledVariableDebt", "type": "uint256"},
                {"internalType": "uint256", "name": "stableBorrowRate", "type": "uint256"},
                {"internalType": "uint256", "name": "liquidityRate", "type": "uint256"},
                {"internalType": "uint40", "name": "stableRateLastUpdated", "type": "uint40"},
                {"internalType": "bool", "name": "usageAsCollateralEnabled", "type": "bool"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
            "name": "getReserveConfigurationData",
            "outputs": [
                {"internalType": "uint256", "name": "decimals", "type": "uint256"},
                {"internalType": "uint256", "name": "ltv", "type": "uint256"},
                {"internalType": "uint256", "name": "liquidationThreshold", "type": "uint256"},
                {"internalType": "uint256", "name": "liquidationBonus", "type": "uint256"},
                {"internalType": "uint256", "name": "reserveFactor", "type": "uint256"},
                {"internalType": "bool", "name": "usageAsCollateralEnabled", "type": "bool"},
                {"internalType": "bool", "name": "borrowingEnabled", "type": "bool"},
                {"internalType": "bool", "name": "stableBorrowRateEnabled", "type": "bool"},
                {"internalType": "bool", "name": "isActive", "type": "bool"},
                {"internalType": "bool", "name": "isFrozen", "type": "bool"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    _POOL_ABI: list[dict[str, Any]] = [
        {
            "inputs": [
                {"internalType": "address", "name": "asset", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
                {"internalType": "address", "name": "onBehalfOf", "type": "address"},
                {"internalType": "uint16", "name": "referralCode", "type": "uint16"},
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
                {"internalType": "address", "name": "to", "type": "address"},
            ],
            "name": "withdraw",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]

    def __init__(self, chain: str, w3: Web3, wallet_address: str | None = None):
        super().__init__(chain=chain)
        self.w3 = w3
        self.wallet_address = Web3.to_checksum_address(wallet_address) if wallet_address else None
        self.logger = logging.getLogger("yield-optimizer-bot.protocols.aave")
        self.data_provider = self.w3.eth.contract(address=self._DATA_PROVIDER_ADDRESS, abi=self._DATA_PROVIDER_ABI)
        self.pool = self.w3.eth.contract(address=self._POOL_ADDRESS, abi=self._POOL_ABI)
        self._token_contracts = {
            symbol: self.w3.eth.contract(address=cfg["address"], abi=ERC20_ABI)
            for symbol, cfg in self._ASSETS.items()
        }

    def _asset_config(self, asset_symbol: str) -> dict[str, Any]:
        try:
            return self._ASSETS[asset_symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Asset not supported by Aave on Polygon: {asset_symbol}") from exc

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError("wallet_address is required to build Aave transaction plans")
        return self.wallet_address

    def supported_assets(self) -> list[str]:
        return list(self._ASSETS.keys())

    def get_spender(self, asset_symbol: str) -> str:
        self._asset_config(asset_symbol)
        return self._POOL_ADDRESS

    def get_asset_address(self, asset_symbol: str) -> str:
        return self._asset_config(asset_symbol)["address"]

    @staticmethod
    def _apr_to_apy(apr_decimal: float) -> float:
        if apr_decimal <= 0:
            return 0.0
        return (1 + (apr_decimal / SECONDS_PER_YEAR)) ** SECONDS_PER_YEAR - 1

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        asset = self._asset_config(asset_symbol)

        async def call_reserve_data():
            return await asyncio.to_thread(
                self.data_provider.functions.getReserveData(asset["address"]).call
            )

        reserve_data = await retry_async(
            call_reserve_data,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

        liquidity_rate_ray = int(reserve_data[5])
        raw_apr = liquidity_rate_ray / RAY
        raw_apy = self._apr_to_apy(raw_apr)

        self.logger.info(
            "Aave APY fetched | asset=%s liquidity_rate_ray=%s raw_apy=%.8f",
            asset_symbol.upper(),
            liquidity_rate_ray,
            raw_apy,
        )

        return YieldQuote(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            net_apy=raw_apy,
            raw_apy=raw_apy,
            details={
                "protocol_address": self._POOL_ADDRESS,
                "data_provider_address": self._DATA_PROVIDER_ADDRESS,
                "asset_address": asset["address"],
                "liquidity_rate_ray": liquidity_rate_ray,
                "apr_decimal": raw_apr,
            },
        )

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        asset = self._asset_config(asset_symbol)
        user = Web3.to_checksum_address(wallet_address)
        token = self._token_contracts[asset_symbol.upper()]

        async def fn():
            wallet_balance = await asyncio.to_thread(token.functions.balanceOf(user).call)
            allowance = await asyncio.to_thread(token.functions.allowance(user, self._POOL_ADDRESS).call)
            reserve_data = await asyncio.to_thread(self.data_provider.functions.getUserReserveData(asset["address"], user).call)
            return int(wallet_balance), int(allowance), reserve_data

        wallet_balance, allowance, reserve_data = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

        supplied_balance = int(reserve_data[0])
        return PositionSnapshot(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            wallet_balance_wei=wallet_balance,
            supplied_balance_wei=supplied_balance,
            withdrawable_balance_wei=supplied_balance,
            allowance_wei=allowance,
            shares_balance_wei=supplied_balance,
            details={
                "asset_address": asset["address"],
                "liquidity_rate_ray": int(reserve_data[6]),
                "usage_as_collateral_enabled": bool(reserve_data[8]),
            },
        )

    async def preview_deposit(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        self._asset_config(asset_symbol)
        return PreviewResult(
            asset_symbol=asset_symbol.upper(),
            amount_wei=int(amount_wei),
            expected_shares_wei=int(amount_wei),
            expected_assets_wei=int(amount_wei),
            price_impact_bps=0,
            details={"preview_model": "aave_1_to_1_supply"},
        )

    async def preview_withdraw(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return await self.preview_deposit(asset_symbol, amount_wei)

    async def check_health(self, asset_symbol: str) -> ProtocolHealth:
        asset = self._asset_config(asset_symbol)

        async def fn():
            reserve_data = await asyncio.to_thread(self.data_provider.functions.getReserveData(asset["address"]).call)
            config_data = await asyncio.to_thread(self.data_provider.functions.getReserveConfigurationData(asset["address"]).call)
            return reserve_data, config_data

        reserve_data, config_data = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        liquidity = int(reserve_data[2])
        is_active = bool(config_data[8])
        is_frozen = bool(config_data[9])
        return ProtocolHealth(
            protocol=self.protocol_name,
            asset_symbol=asset_symbol.upper(),
            is_healthy=is_active and liquidity > 0,
            is_paused=(not is_active) or is_frozen,
            liquidity_wei=liquidity,
            risk_score=0.12,
            confidence_score=0.92,
            details={
                "reserve_factor": int(config_data[4]),
                "is_frozen": is_frozen,
                "is_active": is_active,
            },
        )

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        asset = self._asset_config(asset_symbol)
        recipient = self._require_wallet_address()
        tx_data = self.pool.functions.withdraw(
            asset["address"],
            int(amount_wei),
            recipient,
        )._encode_transaction_data()

        return {
            "to": self._POOL_ADDRESS,
            "from": recipient,
            "data": tx_data,
            "value": 0,
            "gas": 450_000,
        }

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        asset = self._asset_config(asset_symbol)
        sender = self._require_wallet_address()
        tx_data = self.pool.functions.supply(
            asset["address"],
            int(amount_wei),
            sender,
            0,
        )._encode_transaction_data()

        return {
            "to": self._POOL_ADDRESS,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 450_000,
        }
