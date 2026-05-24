from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from web3 import Web3

from app.utils.retry import RetryConfig, retry_async

from .base_protocol import BaseProtocol, PositionSnapshot, PreviewResult, ProtocolHealth, YieldQuote

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
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class StargateProtocol(BaseProtocol):
    protocol_name = "stargate"

    _POOLS_URL = "https://stargate.finance/api/v1/pools"
    _ROUTER_ADDRESS = Web3.to_checksum_address("0x45A01E4e04F14f7A4a6702c74187c5F6222033cd")
    _POOL_ADDRESS = Web3.to_checksum_address("0x1205f31718499dBf1fCa446663B532Ef87481fe1")
    _STAKING_CONTRACT_ADDRESS = Web3.to_checksum_address("0x8731d54E9D02c286767d56ac03e8037C07e01e98")
    _ASSETS: dict[str, dict[str, Any]] = {
        "USDC": {
            "address": Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
            "pool_id": 1,
            "decimals": 6,
        }
    }
    _ROUTER_ABI: list[dict[str, Any]] = [
        {
            "inputs": [
                {"internalType": "uint256", "name": "_poolId", "type": "uint256"},
                {"internalType": "uint256", "name": "_amountLD", "type": "uint256"},
                {"internalType": "address", "name": "_to", "type": "address"},
            ],
            "name": "addLiquidity",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [
                {"internalType": "uint16", "name": "_srcPoolId", "type": "uint16"},
                {"internalType": "uint256", "name": "_amountLP", "type": "uint256"},
                {"internalType": "address", "name": "_to", "type": "address"},
            ],
            "name": "instantRedeemLocal",
            "outputs": [{"internalType": "uint256", "name": "amountSD", "type": "uint256"}],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]
    _POOL_ABI: list[dict[str, Any]] = [
        {
            "inputs": [],
            "name": "totalLiquidity",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [],
            "name": "totalSupply",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    def __init__(self, chain: str, w3: Web3, wallet_address: str | None = None, request_timeout_seconds: int = 10):
        super().__init__(chain=chain)
        self.w3 = w3
        self.wallet_address = Web3.to_checksum_address(wallet_address) if wallet_address else None
        self.request_timeout_seconds = request_timeout_seconds
        self.logger = logging.getLogger("yield-optimizer-bot.protocols.stargate")
        self.router = self.w3.eth.contract(address=self._ROUTER_ADDRESS, abi=self._ROUTER_ABI)
        self.pool = self.w3.eth.contract(address=self._POOL_ADDRESS, abi=self._POOL_ABI)
        self.lp_token = self.w3.eth.contract(address=self._POOL_ADDRESS, abi=ERC20_ABI)
        self._token_contracts = {
            symbol: self.w3.eth.contract(address=cfg["address"], abi=ERC20_ABI)
            for symbol, cfg in self._ASSETS.items()
        }

    def _asset_config(self, asset_symbol: str) -> dict[str, Any]:
        try:
            return self._ASSETS[asset_symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Asset not supported by Stargate on Polygon: {asset_symbol}") from exc

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError("wallet_address is required to build Stargate transaction plans")
        return self.wallet_address

    def supported_assets(self) -> list[str]:
        return list(self._ASSETS.keys())

    def get_spender(self, asset_symbol: str) -> str:
        self._asset_config(asset_symbol)
        return self._ROUTER_ADDRESS

    def get_asset_address(self, asset_symbol: str) -> str:
        return self._asset_config(asset_symbol)["address"]

    async def _get_json(self, url: str) -> Any:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)

        async def fetch() -> Any:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.json()

        return await retry_async(
            fetch,
            config=RetryConfig(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0),
            logger=self.logger,
        )

    def _extract_pool_apy(self, payload: Any, asset_symbol: str) -> float:
        asset = self._asset_config(asset_symbol)
        target_pool_id = str(asset["pool_id"])
        target_pool_address = self._POOL_ADDRESS.lower()
        target_asset_address = asset["address"].lower()
        candidates: list[float] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                address = str(node.get("address") or node.get("poolAddress") or node.get("lpTokenAddress") or "").lower()
                token_address = str(node.get("tokenAddress") or node.get("assetAddress") or "").lower()
                pool_id = str(node.get("poolId") or node.get("pool_id") or node.get("id") or "")
                symbols = {str(node.get("symbol", "")).upper(), str(node.get("token", "")).upper(), str(node.get("asset", "")).upper()}
                is_match = (
                    address == target_pool_address
                    or token_address == target_asset_address
                    or pool_id == target_pool_id
                    or "USDC" in symbols
                )
                if is_match:
                    for key in ("apy", "apr", "lpApr", "lpApy", "totalApr", "totalApy"):
                        value = node.get(key)
                        if value is None:
                            continue
                        numeric = float(value)
                        candidates.append(numeric / 100.0 if numeric > 1 else numeric)
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(payload)
        for candidate in candidates:
            if candidate > 0:
                return candidate
        if candidates:
            return candidates[0]
        raise RuntimeError("Stargate pools payload did not contain APY for Polygon USDC")

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        try:
            payload = await self._get_json(self._POOLS_URL)
            raw_apy = self._extract_pool_apy(payload, asset_symbol)
            source = self._POOLS_URL
        except Exception as exc:
            self.logger.warning(
                "Stargate APY endpoint unavailable, defaulting to zero | asset=%s url=%s error=%s",
                asset_symbol.upper(),
                self._POOLS_URL,
                exc,
            )
            raw_apy = 0.0
            source = "fallback_zero"

        self.logger.info(
            "Stargate APY fetched | asset=%s pool=%s raw_apy=%.8f source=%s",
            asset_symbol.upper(),
            self._POOL_ADDRESS,
            raw_apy,
            source,
        )

        return YieldQuote(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            net_apy=raw_apy,
            raw_apy=raw_apy,
            details={
                "pool_address": self._POOL_ADDRESS,
                "router_address": self._ROUTER_ADDRESS,
                "staking_contract_address": self._STAKING_CONTRACT_ADDRESS,
                "source": source,
            },
        )

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        asset = self._asset_config(asset_symbol)
        token = self._token_contracts[asset_symbol.upper()]
        user = Web3.to_checksum_address(wallet_address)

        async def fn():
            wallet_balance = await asyncio.to_thread(token.functions.balanceOf(user).call)
            allowance = await asyncio.to_thread(token.functions.allowance(user, self._ROUTER_ADDRESS).call)
            lp_balance = await asyncio.to_thread(self.lp_token.functions.balanceOf(user).call)
            total_supply = await asyncio.to_thread(self.lp_token.functions.totalSupply().call)
            total_liquidity = await asyncio.to_thread(self.pool.functions.totalLiquidity().call)
            return int(wallet_balance), int(allowance), int(lp_balance), int(total_supply), int(total_liquidity)

        wallet_balance, allowance, lp_balance, total_supply, total_liquidity = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        supplied_balance = 0 if total_supply == 0 else (lp_balance * total_liquidity) // total_supply

        return PositionSnapshot(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            wallet_balance_wei=wallet_balance,
            supplied_balance_wei=supplied_balance,
            withdrawable_balance_wei=supplied_balance,
            allowance_wei=allowance,
            shares_balance_wei=lp_balance,
            details={
                "pool_id": asset["pool_id"],
                "pool_address": self._POOL_ADDRESS,
                "router_address": self._ROUTER_ADDRESS,
                "total_liquidity_wei": total_liquidity,
                "lp_total_supply_wei": total_supply,
            },
        )

    async def preview_deposit(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        self._asset_config(asset_symbol)
        return PreviewResult(
            asset_symbol=asset_symbol.upper(),
            amount_wei=int(amount_wei),
            expected_shares_wei=int(amount_wei),
            expected_assets_wei=int(amount_wei),
            price_impact_bps=5,
            details={"preview_model": "stargate_lp_1_to_1"},
        )

    async def preview_withdraw(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return await self.preview_deposit(asset_symbol, amount_wei)

    async def check_health(self, asset_symbol: str) -> ProtocolHealth:
        self._asset_config(asset_symbol)

        async def fn():
            total_liquidity = await asyncio.to_thread(self.pool.functions.totalLiquidity().call)
            total_supply = await asyncio.to_thread(self.pool.functions.totalSupply().call)
            return int(total_liquidity), int(total_supply)

        total_liquidity, total_supply = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

        return ProtocolHealth(
            protocol=self.protocol_name,
            asset_symbol=asset_symbol.upper(),
            is_healthy=total_liquidity > 0 and total_supply > 0,
            is_paused=False,
            liquidity_wei=total_liquidity,
            risk_score=0.35,
            confidence_score=0.55,
            details={
                "pool_address": self._POOL_ADDRESS,
                "router_address": self._ROUTER_ADDRESS,
                "total_supply_wei": total_supply,
            },
        )

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        sender = self._require_wallet_address()
        asset = self._asset_config(asset_symbol)
        tx_data = self.router.functions.instantRedeemLocal(
            int(asset["pool_id"]),
            int(amount_wei),
            sender,
        )._encode_transaction_data()
        return {
            "to": self._ROUTER_ADDRESS,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 650_000,
        }

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        sender = self._require_wallet_address()
        asset = self._asset_config(asset_symbol)
        tx_data = self.router.functions.addLiquidity(
            int(asset["pool_id"]),
            int(amount_wei),
            sender,
        )._encode_transaction_data()
        return {
            "to": self._ROUTER_ADDRESS,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 650_000,
        }
