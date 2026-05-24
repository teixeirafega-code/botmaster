from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from web3 import Web3

from app.utils.retry import RetryConfig, retry_async


ERC20_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}, {"internalType": "address", "name": "spender", "type": "address"}],
        "name": "allowance",
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
        "inputs": [{"internalType": "address", "name": "spender", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str = "dynamic"
    exact_allowance: bool = False
    min_refresh_ratio: float = 0.95
    infinite_approval_value: int = (2**256) - 1


@dataclass(frozen=True)
class AllowanceCheckResult:
    token_address: str
    owner: str
    spender: str
    current_allowance_wei: int
    required_allowance_wei: int
    approval_required: bool
    approval_amount_wei: int


class ApprovalManager:
    def __init__(self, w3: Web3, owner_address: str, whitelisted_spenders: set[str], policy: ApprovalPolicy | None = None):
        self.w3 = w3
        self.owner_address = Web3.to_checksum_address(owner_address)
        self.whitelisted_spenders = {Web3.to_checksum_address(spender) for spender in whitelisted_spenders}
        self.policy = policy or ApprovalPolicy()
        self.logger = logging.getLogger("yield-optimizer-bot.blockchain.approval_manager")

    def _token_contract(self, token_address: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)

    def _validate_spender(self, spender: str) -> str:
        checksum_spender = Web3.to_checksum_address(spender)
        if checksum_spender not in self.whitelisted_spenders:
            raise ValueError(f"Spender not whitelisted for approvals: {checksum_spender}")
        return checksum_spender

    async def get_allowance(self, token_address: str, spender: str) -> int:
        checksum_spender = self._validate_spender(spender)
        token = self._token_contract(token_address)

        async def fn() -> int:
            return int(await asyncio.to_thread(token.functions.allowance(self.owner_address, checksum_spender).call))

        return await retry_async(fn, RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0), logger=self.logger)

    async def get_balance(self, token_address: str) -> int:
        token = self._token_contract(token_address)

        async def fn() -> int:
            return int(await asyncio.to_thread(token.functions.balanceOf(self.owner_address).call))

        return await retry_async(fn, RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0), logger=self.logger)

    async def check_allowance(self, token_address: str, spender: str, required_allowance_wei: int) -> AllowanceCheckResult:
        current_allowance = await self.get_allowance(token_address, spender)

        if self.policy.mode == "infinite":
            approval_amount = self.policy.infinite_approval_value
            approval_required = current_allowance < required_allowance_wei
        elif self.policy.exact_allowance:
            approval_amount = int(required_allowance_wei)
            approval_required = current_allowance != required_allowance_wei
        else:
            refresh_threshold = int(required_allowance_wei * self.policy.min_refresh_ratio)
            approval_amount = int(required_allowance_wei)
            approval_required = current_allowance < max(1, refresh_threshold)

        return AllowanceCheckResult(
            token_address=Web3.to_checksum_address(token_address),
            owner=self.owner_address,
            spender=Web3.to_checksum_address(spender),
            current_allowance_wei=current_allowance,
            required_allowance_wei=int(required_allowance_wei),
            approval_required=approval_required,
            approval_amount_wei=approval_amount,
        )

    def build_approval_tx(self, token_address: str, spender: str, amount_wei: int) -> dict[str, Any]:
        checksum_spender = self._validate_spender(spender)
        token = self._token_contract(token_address)
        data = token.functions.approve(checksum_spender, int(amount_wei))._encode_transaction_data()
        return {
            "to": Web3.to_checksum_address(token_address),
            "from": self.owner_address,
            "data": data,
            "value": 0,
            "gas": 120_000,
        }

    def build_revoke_tx(self, token_address: str, spender: str) -> dict[str, Any]:
        return self.build_approval_tx(token_address=token_address, spender=spender, amount_wei=0)
