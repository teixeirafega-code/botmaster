from __future__ import annotations

import asyncio
import json
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


class BeefyProtocol(BaseProtocol):
    protocol_name = "beefy"

    _APY_URL = "https://api.beefy.finance/apy"
    _VAULTS_URL = "https://api.beefy.finance/vaults"
    _USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    _ASSETS: dict[str, dict[str, Any]] = {
        "USDC": {
            "address": _USDC_ADDRESS,
            "decimals": 6,
        }
    }
    _VAULT_ABI: list[dict[str, Any]] = [
        {
            "inputs": [{"internalType": "uint256", "name": "_amount", "type": "uint256"}],
            "name": "deposit",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "uint256", "name": "_shares", "type": "uint256"}],
            "name": "withdraw",
            "outputs": [],
            "stateMutability": "nonpayable",
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
            "inputs": [],
            "name": "balance",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [],
            "name": "getPricePerFullShare",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [],
            "name": "paused",
            "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
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
    _VAULT_APY_ALIASES: dict[str, tuple[str, ...]] = {
        "aave-usdc-eol": ("aave-usdc-eol", "aave-usdc"),
        "compound-polygon-usdc": ("compound-polygon-usdc",),
    }

    def __init__(self, chain: str, w3: Web3, wallet_address: str | None = None, request_timeout_seconds: int = 10):
        super().__init__(chain=chain)
        self.w3 = w3
        self.wallet_address = Web3.to_checksum_address(wallet_address) if wallet_address else None
        self.request_timeout_seconds = request_timeout_seconds
        self.logger = logging.getLogger("yield-optimizer-bot.protocols.beefy")
        self._token_contracts = {
            symbol: self.w3.eth.contract(address=cfg["address"], abi=ERC20_ABI)
            for symbol, cfg in self._ASSETS.items()
        }

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError("wallet_address is required to build Beefy transaction plans")
        return self.wallet_address

    def supported_assets(self) -> list[str]:
        return list(self._ASSETS.keys())

    def get_spender(self, asset_symbol: str) -> str:
        vault = self._resolve_vault_sync(asset_symbol)
        return Web3.to_checksum_address(vault["earnContractAddress"])

    def get_asset_address(self, asset_symbol: str) -> str:
        return self._ASSETS[asset_symbol.upper()]["address"]

    async def _get_text(self, url: str) -> str:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)

        async def fetch() -> str:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.text()

        return await retry_async(
            fetch,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

    async def _get_json(self, url: str) -> Any:
        payload = await self._get_text(url)
        return json.loads(payload)

    def _candidate_apy_keys(self, vault_id: str) -> tuple[str, ...]:
        return self._VAULT_APY_ALIASES.get(vault_id, (vault_id,))

    def _select_vault_from_payloads(self, asset_symbol: str, vaults_payload: list[dict[str, Any]], apy_payload: dict[str, Any]) -> dict[str, Any]:
        asset_address = self.get_asset_address(asset_symbol).lower()
        candidates = [
            vault
            for vault in vaults_payload
            if str(vault.get("network", "")).lower() == "polygon"
            and str(vault.get("tokenAddress", "")).lower() == asset_address
            and (
                str(vault.get("earnedToken", "")).startswith("moo")
                or bool(vault.get("earnContractAddress"))
            )
        ]
        if not candidates:
            raise RuntimeError("Beefy did not return any Polygon USDC vault candidates")

        def score(vault: dict[str, Any]) -> tuple[float, int, int]:
            apy = 0.0
            for key in self._candidate_apy_keys(str(vault.get("id", ""))):
                value = apy_payload.get(key)
                if value is None:
                    continue
                apy = max(apy, float(value))
            active_bonus = 1 if str(vault.get("status", "")).lower() == "active" else 0
            single_asset_bonus = 1 if len(vault.get("assets") or []) == 1 else 0
            return (apy, active_bonus, single_asset_bonus)

        best = max(candidates, key=score)
        best = dict(best)
        best["_selected_apy"] = score(best)[0]
        return best

    async def _resolve_vault(self, asset_symbol: str) -> dict[str, Any]:
        apy_payload, vaults_payload = await asyncio.gather(
            self._get_json(self._APY_URL),
            self._get_json(self._VAULTS_URL),
        )
        return self._select_vault_from_payloads(asset_symbol, vaults_payload, apy_payload)

    def _resolve_vault_sync(self, asset_symbol: str) -> dict[str, Any]:
        if asset_symbol.upper() != "USDC":
            raise ValueError(f"Asset not supported by Beefy on Polygon: {asset_symbol}")
        return {
            "id": "aave-usdc-eol",
            "name": "USDC.e",
            "token": "USDC.e",
            "earnedToken": "mooAaveUSDC",
            "earnContractAddress": Web3.to_checksum_address("0xE71f3C11D4535a7F8c5FB03FDA57899B2C9c721F"),
            "tokenAddress": self._USDC_ADDRESS,
            "status": "eol",
            "assets": ["pUSDCe"],
        }

    def _vault_contract(self, vault: dict[str, Any]):
        return self.w3.eth.contract(address=Web3.to_checksum_address(vault["earnContractAddress"]), abi=self._VAULT_ABI)

    async def fetch_apy(self, asset_symbol: str) -> YieldQuote:
        vault = await self._resolve_vault(asset_symbol)
        raw_apy = float(vault.get("_selected_apy", 0.0))

        self.logger.info(
            "Beefy APY fetched | asset=%s vault=%s raw_apy=%.8f status=%s",
            asset_symbol.upper(),
            vault["id"],
            raw_apy,
            vault.get("status"),
        )

        return YieldQuote(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            net_apy=raw_apy,
            raw_apy=raw_apy,
            details={
                "vault_id": vault["id"],
                "vault_name": vault.get("name"),
                "vault_address": Web3.to_checksum_address(vault["earnContractAddress"]),
                "vault_status": vault.get("status"),
                "underlying_token_address": Web3.to_checksum_address(vault["tokenAddress"]),
            },
        )

    async def discover_position(self, wallet_address: str, asset_symbol: str) -> PositionSnapshot:
        vault = self._resolve_vault_sync(asset_symbol)
        vault_contract = self._vault_contract(vault)
        token = self._token_contracts[asset_symbol.upper()]
        user = Web3.to_checksum_address(wallet_address)

        async def fn():
            wallet_balance = await asyncio.to_thread(token.functions.balanceOf(user).call)
            allowance = await asyncio.to_thread(token.functions.allowance(user, vault_contract.address).call)
            shares_balance = await asyncio.to_thread(vault_contract.functions.balanceOf(user).call)
            total_supply = await asyncio.to_thread(vault_contract.functions.totalSupply().call)
            vault_balance = await asyncio.to_thread(vault_contract.functions.balance().call)
            return int(wallet_balance), int(allowance), int(shares_balance), int(total_supply), int(vault_balance)

        wallet_balance, allowance, shares_balance, total_supply, vault_balance = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )
        supplied_balance = 0 if total_supply == 0 else (shares_balance * vault_balance) // total_supply

        return PositionSnapshot(
            protocol=self.protocol_name,
            chain=self.chain,
            asset_symbol=asset_symbol.upper(),
            wallet_balance_wei=wallet_balance,
            supplied_balance_wei=supplied_balance,
            withdrawable_balance_wei=supplied_balance,
            allowance_wei=allowance,
            shares_balance_wei=shares_balance,
            details={
                "vault_id": vault["id"],
                "vault_address": vault_contract.address,
                "vault_total_underlying_wei": vault_balance,
                "vault_total_supply_wei": total_supply,
            },
        )

    async def preview_deposit(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return PreviewResult(
            asset_symbol=asset_symbol.upper(),
            amount_wei=int(amount_wei),
            expected_shares_wei=int(amount_wei),
            expected_assets_wei=int(amount_wei),
            price_impact_bps=0,
            details={"preview_model": "beefy_vault_single_asset"},
        )

    async def preview_withdraw(self, asset_symbol: str, amount_wei: int) -> PreviewResult:
        return await self.preview_deposit(asset_symbol, amount_wei)

    async def check_health(self, asset_symbol: str) -> ProtocolHealth:
        vault = self._resolve_vault_sync(asset_symbol)
        vault_contract = self._vault_contract(vault)

        async def fn():
            vault_balance = await asyncio.to_thread(vault_contract.functions.balance().call)
            total_supply = await asyncio.to_thread(vault_contract.functions.totalSupply().call)
            try:
                paused = bool(await asyncio.to_thread(vault_contract.functions.paused().call))
            except Exception:
                paused = False
            return int(vault_balance), int(total_supply), paused

        vault_balance, total_supply, paused = await retry_async(
            fn,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

        return ProtocolHealth(
            protocol=self.protocol_name,
            asset_symbol=asset_symbol.upper(),
            is_healthy=(not paused) and vault_balance > 0 and total_supply >= 0,
            is_paused=paused,
            liquidity_wei=vault_balance,
            risk_score=0.4,
            confidence_score=0.68,
            details={
                "vault_id": vault["id"],
                "vault_status": vault.get("status"),
                "vault_address": vault_contract.address,
                "vault_total_supply_wei": total_supply,
            },
        )

    def build_withdraw_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        sender = self._require_wallet_address()
        vault = self._resolve_vault_sync(asset_symbol)
        vault_contract = self._vault_contract(vault)
        tx_data = vault_contract.functions.withdraw(int(amount_wei))._encode_transaction_data()
        return {
            "to": vault_contract.address,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 500_000,
        }

    def build_deposit_plan(self, asset_symbol: str, amount_wei: int) -> dict[str, Any]:
        sender = self._require_wallet_address()
        vault = self._resolve_vault_sync(asset_symbol)
        vault_contract = self._vault_contract(vault)
        tx_data = vault_contract.functions.deposit(int(amount_wei))._encode_transaction_data()
        return {
            "to": vault_contract.address,
            "from": sender,
            "data": tx_data,
            "value": 0,
            "gas": 500_000,
        }
