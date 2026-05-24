from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from web3 import Web3


@dataclass(frozen=True)
class TxPlan:
    tx: dict[str, Any]
    description: str


@dataclass(frozen=True)
class BroadcastPolicy:
    receipt_timeout_seconds: int = 300
    max_broadcast_attempts: int = 3
    gas_escalation_multiplier: float = 1.125


@dataclass(frozen=True)
class SentTransaction:
    tx_hash: Optional[str]
    nonce: int
    tx: dict[str, Any]
    receipt: dict[str, Any] | None = None
    simulated: bool = False
    gas_estimate: int | None = None
    simulation_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionMode:
    dry_run: bool
    execute_transactions: bool
    sign_transactions: bool
    paper_trading: bool = False

    @property
    def broadcasting_enabled(self) -> bool:
        return self.execute_transactions and not self.dry_run

    @property
    def signing_enabled(self) -> bool:
        return self.sign_transactions and not self.dry_run


class TransactionManager:
    def __init__(self, w3: Web3, account, chain_id: int, execution_mode: ExecutionMode):
        self.w3 = w3
        self.account = account
        self.chain_id = chain_id
        self.execution_mode = execution_mode
        self.logger = logging.getLogger("yield-optimizer-bot.blockchain.transaction_manager")
        self._next_nonce: int | None = None
        self._assert_safe_mode()

    def _assert_safe_mode(self) -> None:
        if self.execution_mode.dry_run:
            if self.execution_mode.execute_transactions:
                raise RuntimeError("Dry-run mode must not enable transaction execution")
            if self.execution_mode.sign_transactions:
                raise RuntimeError("Dry-run mode must not enable transaction signing")

    def _reserve_nonce(self) -> int:
        chain_nonce = int(self.w3.eth.get_transaction_count(self.account.address, "pending"))
        if self._next_nonce is None or self._next_nonce < chain_nonce:
            self._next_nonce = chain_nonce
        nonce = self._next_nonce
        self._next_nonce += 1
        return nonce

    @staticmethod
    def _escalate_fee(tx: dict[str, Any], multiplier: float) -> dict[str, Any]:
        updated = dict(tx)
        if "maxFeePerGas" in updated:
            updated["maxFeePerGas"] = int(updated["maxFeePerGas"] * multiplier)
        if "maxPriorityFeePerGas" in updated:
            updated["maxPriorityFeePerGas"] = int(updated["maxPriorityFeePerGas"] * multiplier)
        if "gasPrice" in updated:
            updated["gasPrice"] = int(updated["gasPrice"] * multiplier)
        return updated

    def build_and_validate(self, plan: TxPlan, forced_nonce: int | None = None) -> TxPlan:
        tx = dict(plan.tx)
        tx.setdefault("chainId", self.chain_id)
        if "from" in tx:
            tx.pop("from", None)

        tx["nonce"] = forced_nonce if forced_nonce is not None else tx.get("nonce", self._reserve_nonce())
        tx.setdefault("gas", 300_000)

        latest_block = self.w3.eth.get_block("latest")
        if latest_block.get("baseFeePerGas") is not None:
            tx.setdefault("maxPriorityFeePerGas", tx.get("maxPriorityFeePerGas", 0))
            tx.setdefault("maxFeePerGas", tx.get("maxFeePerGas", 0))
        else:
            tx.setdefault("gasPrice", tx.get("gasPrice", self.w3.eth.gas_price))

        if tx.get("to") is None and tx.get("data"):
            raise ValueError("Unsupported tx: contract creation")

        return TxPlan(tx=tx, description=plan.description)

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        try:
            return int(self.w3.eth.estimate_gas(tx))
        except Exception:  # noqa: BLE001
            return int(tx.get("gas", 300_000))

    def dry_run_simulate(self, tx: dict[str, Any]) -> dict[str, Any]:
        gas_estimate = self.estimate_gas(tx)
        call_result = self.w3.eth.call(tx, "latest")
        return {
            "gas_estimate": gas_estimate,
            "call_result_hex": call_result.hex() if hasattr(call_result, "hex") else str(call_result),
        }

    def validate_receipt(self, receipt: dict[str, Any]) -> None:
        if int(receipt.get("status", 0)) != 1:
            raise RuntimeError(f"Transaction reverted on-chain: {receipt}")

    def _simulated_tx_hash(self, tx: dict[str, Any], description: str) -> str:
        digest = hashlib.sha256(
            json.dumps({"tx": tx, "description": description}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return "0x" + digest

    def _simulated_receipt(self, tx_hash: str, tx: dict[str, Any], gas_estimate: int) -> dict[str, Any]:
        block_number = int(self.w3.eth.block_number)
        gas_price = int(tx.get("gasPrice") or tx.get("maxFeePerGas") or 0)
        return {
            "transactionHash": tx_hash,
            "status": 1,
            "blockNumber": block_number,
            "gasUsed": gas_estimate,
            "effectiveGasPrice": gas_price,
            "logs": [],
            "simulated": True,
            "to": tx.get("to"),
            "nonce": tx.get("nonce"),
        }

    def sign_and_send(self, plan: TxPlan, forced_nonce: int | None = None) -> Optional[str]:
        valid_plan = self.build_and_validate(plan, forced_nonce=forced_nonce)
        if not self.execution_mode.signing_enabled or not self.execution_mode.broadcasting_enabled:
            raise RuntimeError("Real sign_and_send is disabled in current execution mode")

        signed = self.account.sign_transaction(valid_plan.tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        return tx_hash.hex()

    def wait_receipt(self, tx_hash: str, timeout_seconds: int = 300) -> dict[str, Any]:
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_seconds)
        self.validate_receipt(receipt)
        return receipt

    def execute_plan(self, plan: TxPlan, policy: BroadcastPolicy | None = None) -> SentTransaction:
        execution_policy = policy or BroadcastPolicy()
        last_error: Exception | None = None
        nonce = self._reserve_nonce()
        base_plan = self.build_and_validate(plan, forced_nonce=nonce)

        simulation = self.dry_run_simulate(base_plan.tx)
        gas_estimate = int(simulation["gas_estimate"])

        if self.execution_mode.dry_run or not self.execution_mode.broadcasting_enabled:
            tx_hash = self._simulated_tx_hash(base_plan.tx, plan.description)
            receipt = self._simulated_receipt(tx_hash, base_plan.tx, gas_estimate)
            return SentTransaction(
                tx_hash=tx_hash,
                nonce=nonce,
                tx=base_plan.tx,
                receipt=receipt,
                simulated=True,
                gas_estimate=gas_estimate,
                simulation_details=simulation,
            )

        current_tx = dict(base_plan.tx)
        for attempt in range(1, execution_policy.max_broadcast_attempts + 1):
            try:
                if not self.execution_mode.signing_enabled:
                    raise RuntimeError("Signing disabled in execution mode")
                signed = self.account.sign_transaction(current_tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction).hex()
                receipt = self.wait_receipt(tx_hash, timeout_seconds=execution_policy.receipt_timeout_seconds)
                return SentTransaction(
                    tx_hash=tx_hash,
                    nonce=nonce,
                    tx=current_tx,
                    receipt=receipt,
                    simulated=False,
                    gas_estimate=gas_estimate,
                    simulation_details=simulation,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.logger.warning(
                    "Broadcast attempt failed | description=%s nonce=%s attempt=%s/%s error=%s",
                    plan.description,
                    nonce,
                    attempt,
                    execution_policy.max_broadcast_attempts,
                    exc,
                )
                if attempt >= execution_policy.max_broadcast_attempts:
                    break
                current_tx = self._escalate_fee(current_tx, execution_policy.gas_escalation_multiplier)
                time.sleep(1.0)

        assert last_error is not None
        raise last_error
