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


class CurveProtocol(BaseProtocol):
    protocol_name = "curve"

    _POOLS_URL = "https://api.curve.finance/api/getPools/polygon/main"
    _VOLUMES_URL = "https://api.curve.finance/v1/getVolumes/polygon"
    _POOL_ADDRESS = Web3.to_checksum_address("0x445FE580eF8d70FF569aB36e80c647af338db351")
    _LP_TOKEN_ADDRESS = Web3.to_checksum_address("0xE7a24EF0C5e95Ffb0f6684b813A78F2a3AD7D171")

    _ASSETS: dict[str, dict[str, Any]] = {
        "USDC": {
            "address": Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
            "index": 1,
            "curve_coin_address": Web3.to_checksum_address("0x1a13F4Ca1d028320A707D99520AbFefca3998b7F"),
        },
        "USDT": {
            "address": Web3.to_checksum_address("0xc2132D05D31c914a87C6611C10748AEb04B58e8F"),
            "index": 2,
            "curve_coin_address": Web3.to_checksum_address("0x60D55F02A771d515e077c9C2403a1ef324885CeC"),
        },
    }

    _POOL_ABI: list[dict[str, Any]] = [
        {
            "inputs": [
                {"internalType": "uint256[3]", "name": "amounts", "type": "uint256[3]"},
                {"internalType": "bool", "name": "is_deposit", "type": "bool"},
            ],
            "name": "calc_token_amount",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "int128", "name": "i", "type": "int128"}],
            "name": "balances",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [
                {"internalType": "uint256[3]", "name": "amounts", "type": "uint256[3]"},
                {"internalType": "uint256", "name": "min_mint_amount", "type": "uint256"},
                {"internalType": "bool", "name": "use_underlying", "type": "bool"},
            ],
            "name": "add_liquidity",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [
                {"internalType": "uint256[3]", "name": "amounts", "type": "uint256[3]"},
                {"internalType": "uint256", "name": "max_burn_amount", "type": "uint256"},
                {"internalType": "bool", "name": "use_underlying", "type": "bool"},
            ],
            "name": "remove_liquidity_imbalance",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]
    _POOL_BALANCES_UINT256_ABI: list[dict[str, Any]] = [
        {
            "inputs": [{"internalType": "uint256", "name": "i", "type": "uint256"}],
            "name": "balances",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]

    def __init__(self, chain: str, w3: Web3, wallet_address: str | None = None, request_timeout_seconds: int = 10):
        super().__init__(chain=chain)
        self.w3 = w3
        self.wallet_address = Web3.to_checksum_address(wallet_address) if wallet_address else None
        self.request_timeout_seconds = request_timeout_seconds
        self.logger = logging.getLogger("yield-optimizer-bot.protocols.curve")
        self.pool = self.w3.eth.contract(address=self._POOL_ADDRESS, abi=self._POOL_ABI)
        self.pool_uint256 = self.w3.eth.contract(address=self._POOL_ADDRESS, abi=self._POOL_BALANCES_UINT256_ABI)
        self.lp_token = self.w3.eth.contract(address=self._LP_TOKEN_ADDRESS, abi=ERC20_ABI)
        self._token_contracts = {
            symbol: self.w3.eth.contract(address=cfg["address"], abi=ERC20_ABI)
            for symbol, cfg in self._ASSETS.items()
        }

    def _asset_config(self, asset_symbol: str) -> dict[str, Any]:
        try:
            return self._ASSETS[asset_symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Asset not supported by Curve on Polygon: {asset_symbol}") from exc

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError("wallet_address is required to build Curve transaction plans")
        return self.wallet_address

    def supported_assets(self) -> list[str]:
        return list(self._ASSETS.keys())

    def get_spender(self, asset_symbol: str) -> str:
        self._asset_config(asset_symbol)
        return self._POOL_ADDRESS

    def get_asset_address(self, asset_symbol: str) -> str:
        return self._asset_config(asset_symbol)["address"]

    async def _get_json(self, url: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)

        async def fetch() -> dict[str, Any]:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.json()

        return await retry_async(
            fetch,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

    @staticmethod
    def _normalize_address(value: str | None) -> str | None:
        if not value:
            return None
        return value.lower()

    def _extract_pool(self, pools_payload: dict[str, Any]) -> dict[str, Any]:
        pool_data = pools_payload.get("data", {}).get("poolData", [])
        target_address = self._POOL_ADDRESS.lower()
        for pool in pool_data:
            if self._normalize_address(pool.get("address")) == target_address:
                return pool
        raise RuntimeError("Curve Polygon main pool for USDC/USDT was not found in getPools response")

    def _extract_base_apy(self, volume_payload: dict[str, Any], pool: dict[str, Any]) -> float | None:
        candidates: list[Any] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                address = self._normalize_address(node.get("address"))
                pool_id = str(node.get("pool") or node.get("poolId") or node.get("id") or "")
                if address == self._POOL_ADDRESS.lower() or pool_id == str(pool.get("id", "")):
                    for key in (
                        "apy",
                        "baseApy",
                        "latestDailyApy",
                        "latestBaseApy",
                        "latestDailyApyPcent",
                        "latestWeeklyApyPcent",
                    ):
                        if key in node and node[key] is not None:
                            candidates.append(node[key])
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(volume_payload)

        return self._select_apy_candidate(candidates)

    def _extract_base_apy_from_pool_payload(self, pool: dict[str, Any]) -> float | None:
        candidates: list[Any] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                for key in (
                    "apy",
                    "baseApy",
                    "latestDailyApy",
                    "latestBaseApy",
                    "latestDailyApyPcent",
                    "latestWeeklyApyPcent",
                ):
                    if key in node and node[key] is not None:
                        candidates.append(node[key])
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(pool)
        return self._select_apy_candidate(candidates)

    def _select_apy_candidate(self, candidates: list[Any]) -> float | None:
        normalized: list[float] = []
        for candidate in candidates:
            if isinstance(candidate, dict):
                for key in ("day", "daily", "base", "apy", "weekly"):
                    value = candidate.get(key)
                    if value is None:
                        continue
                    numeric = float(value)
                    normalized.append(numeric / 100.0 if numeric > 1 else numeric)
            else:
                numeric = float(candidate)
                normalized.append(numeric / 100.0 if numeric > 1 else numeric)

        for value in normalized:
            if value > 0:
                return value
        return normalized[0] if normalized else None

    async def _read_pool_asset_balance(self, asset: dict[str, Any]) -> int | None:
        index = int(asset["index"])

        async def read_int128() -> int:
            return int(await asyncio.to_thread(self.pool.functions.balances(index).call))

        async def read_uint256() -> int:
            return int(await asyncio.to_thread(self.pool_uint256.functions.balances(index).call))

        for reader in (read_int128, read_uint256):
            try:
                return await retry_async(
                    reader,
                    config=RetryConfig(max_attempts=2, base_delay_seconds=0.2, max_delay_seconds=1.0),
                    logger=self.logger,
                )
            except Exception:
                continue
        return None

    def _extract_pool_coin_balance(self, pool: dict[str, Any], asset: dict[str, Any]) -> int | None:
        target_addresses = {
            self._normalize_address(asset.get("address")),
            self._normalize_address(asset.get("curve_coin_address")),
        }
        candidates: list[Any] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                address = self._normalize_address(
                    node.get("address") or node.get("tokenAddress") or node.get("underlyingAddress")
                )
                symbol = str(node.get("symbol", "")).upper()
                if address in target_addresses or symbol in {"USDC", "USDT"}:
                    for key in ("poolBalance", "balance", "balanceAmount", "poolTokenBalance", "usdTotalExcludingBasePool"):
                        value = node.get(key)
                        if value is not None:
                            candidates.append(value)
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(pool)

        for candidate in candidates:
            try:
                numeric = float(candidate)
            except (TypeError, ValueError):
                continue
            if numeric <= 0:
                continue
            if numeric.is_integer() and numeric > 1_000_000:
                return int(numeric)
            return int(numeric * 10**6)
        return None

    async def _pool_asset_balance_with_fallback(self, asset: dict[str, Any], pool: dict[str, Any] | None = None) -> int:
        pool_asset_balance = await self._read_pool_asset_balance(asset)
        if pool_asset_balance is not None:
            return pool_asset_balance

        if pool is None:
            try:
                pool = self._extract_pool(await self._get_json(self._POOLS_URL))
            except Exception:
                pool = None

        if pool is not None:
            payload_balance = self._extract_pool_coin_balance(pool, asset)
            if payload_balance is not None:
                self.logger.warning(
                    "Curve pool balance fallback via API payload | asset=%s pool=%s balance=%s",
                    next((symbol for symbol, cfg in self._ASSETS.items() if cfg is asset), "unknown"),
                    self._POOL_ADDRESS,
                    payload_balance,
                )
                return payload_balance

        self.logger.warning(
            "Curve pool balance unavailable after on-chain and API fallbacks | asset=%s pool=%s",
            next((symbol for symbol, cfg in self._ASSETS.items() if cfg is asset), "unknown"),
            self._POOL_ADDRESS,
        )
        return 0

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        asset = self._asset_config(asset_symbol)
        pools_payload, volume_payload = await asyncio.gather(
            self._get_json(self._POOLS_URL),
            self._get_json(self._VOLUMES_URL),
        )

        pool = self._extract_pool(pools_payload)
        base_apy = self._extract_base_apy(volume_payload, pool)
        if base_apy is None:
            base_apy = self._extract_base_apy_from_pool_payload(pool)

        if base_apy is None:
            raise RuntimeError("Curve volume/APY payload did not contain base APY for the target Polygon pool")

        self.logger.info(
            "Curve APY fetched | asset=%s pool=%s base_apy=%.8f",
            asset_symbol.upper(),
            self._POOL_ADDRESS,
            base_apy,
        )

        return YieldQuote(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            net_apy=base_apy,
            raw_apy=base_apy,
            details={
                "pool_address": self._POOL_ADDRESS,
                "curve_coin_address": asset["curve_coin_address"],
                "pool_name": pool.get("name"),
                "lp_token_address": pool.get("lpTokenAddress"),
                "gauge_address": pool.get("gaugeAddress"),
                "usd_total": pool.get("usdTotal"),
            },
        )

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        asset = self._asset_config(asset_symbol)
        user = Web3.to_checksum_address(wallet_address)
        token = self._token_contracts[asset_symbol.upper()]

        async def fn():
            wallet_balance = await asyncio.to_thread(token.functions.balanceOf(user).call)
            allowance = await asyncio.to_thread(token.functions.allowance(user, self._POOL_ADDRESS).call)
            lp_balance = await asyncio.to_thread(self.lp_token.functions.balanceOf(user).call)
            lp_total_supply = await asyncio.to_thread(self.lp_token.functions.totalSupply().call)
            pool_asset_balance = 0
            if int(lp_balance) > 0 and int(lp_total_supply) > 0:
                pool_asset_balance = await self._pool_asset_balance_with_fallback(asset)
            return int(wallet_balance), int(allowance), int(lp_balance), int(lp_total_supply), int(pool_asset_balance)

        wallet_balance, allowance, lp_balance, lp_total_supply, pool_asset_balance = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        supplied_balance = 0 if lp_total_supply == 0 else (lp_balance * pool_asset_balance) // lp_total_supply
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
                "lp_token_address": self._LP_TOKEN_ADDRESS,
                "pool_balance_for_asset": pool_asset_balance,
                "lp_total_supply": lp_total_supply,
            },
        )

    async def preview_deposit(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        amounts = self._amounts_for_asset(asset_symbol, amount_wei)

        async def fn():
            return int(await asyncio.to_thread(self.pool.functions.calc_token_amount(amounts, True).call))

        expected_lp = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        return PreviewResult(
            asset_symbol=asset_symbol.upper(),
            amount_wei=int(amount_wei),
            expected_shares_wei=expected_lp,
            expected_assets_wei=int(amount_wei),
            price_impact_bps=10,
            details={"preview_model": "curve_calc_token_amount_deposit"},
        )

    async def preview_withdraw(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        amounts = self._amounts_for_asset(asset_symbol, amount_wei)

        async def fn():
            return int(await asyncio.to_thread(self.pool.functions.calc_token_amount(amounts, False).call))

        expected_burn = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        return PreviewResult(
            asset_symbol=asset_symbol.upper(),
            amount_wei=int(amount_wei),
            expected_shares_wei=expected_burn,
            expected_assets_wei=int(amount_wei),
            price_impact_bps=12,
            details={"preview_model": "curve_calc_token_amount_withdraw"},
        )

    async def check_health(self, asset_symbol: str) -> ProtocolHealth:
        asset = self._asset_config(asset_symbol)
        lp_total_supply = int(
            await retry_async(
                lambda: asyncio.to_thread(self.lp_token.functions.totalSupply().call),
                config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
                logger=self.logger,
            )
        )
        pool = None
        try:
            pool = self._extract_pool(await self._get_json(self._POOLS_URL))
        except Exception:
            pool = None
        asset_balance = await self._pool_asset_balance_with_fallback(asset, pool=pool)
        if asset_balance <= 0 and pool is not None:
            usd_total = pool.get("usdTotal")
            try:
                if usd_total is not None and float(usd_total) > 0:
                    asset_balance = int(float(usd_total) * 10**6)
            except (TypeError, ValueError):
                pass
        return ProtocolHealth(
            protocol=self.protocol_name,
            asset_symbol=asset_symbol.upper(),
            is_healthy=asset_balance > 0 and lp_total_supply > 0,
            is_paused=False,
            liquidity_wei=asset_balance,
            risk_score=0.25,
            confidence_score=0.8,
            details={"lp_total_supply": lp_total_supply, "pool_address": self._POOL_ADDRESS},
        )

    def _amounts_for_asset(self, asset_symbol: str, amount_wei: int) -> list[int]:
        asset = self._asset_config(asset_symbol)
        amounts = [0, 0, 0]
        amounts[int(asset["index"])] = int(amount_wei)
        return amounts

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        sender = self._require_wallet_address()
        amounts = self._amounts_for_asset(asset_symbol, amount_wei)
        quoted_lp_burn = int(
            self.pool.functions.calc_token_amount(amounts, False).call()
        )
        max_burn_amount = (quoted_lp_burn * 10_100) // 10_000
        tx_data = self.pool.functions.remove_liquidity_imbalance(
            amounts,
            max_burn_amount,
            True,
        )._encode_transaction_data()

        return {
            "to": self._POOL_ADDRESS,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 650_000,
        }

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        sender = self._require_wallet_address()
        amounts = self._amounts_for_asset(asset_symbol, amount_wei)
        quoted_lp_mint = int(
            self.pool.functions.calc_token_amount(amounts, True).call()
        )
        min_mint_amount = (quoted_lp_mint * 9_900) // 10_000
        tx_data = self.pool.functions.add_liquidity(
            amounts,
            min_mint_amount,
            True,
        )._encode_transaction_data()

        return {
            "to": self._POOL_ADDRESS,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 650_000,
        }
