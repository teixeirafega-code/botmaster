from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.blockchain.approval_manager import ApprovalManager
from app.blockchain.transaction_manager import BroadcastPolicy, TransactionManager, TxPlan
from app.protocols.base_protocol import BaseProtocol, PositionSnapshot
from app.services.execution_journal import ExecutionJournal
from app.services.paper_portfolio import PaperPortfolioLedger
from app.services.portfolio_manager import PortfolioManager
from app.services.profitability_engine import ProfitabilityReport
from app.services.reconciliation import ReconciliationService


@dataclass(frozen=True)
class RebalancePlan:
    withdraw_protocol: str | None
    deposit_protocol: str
    asset_symbol: str
    amount_wei: int
    profitability: ProfitabilityReport | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        payload = {
            "withdraw_protocol": self.withdraw_protocol,
            "deposit_protocol": self.deposit_protocol,
            "asset_symbol": self.asset_symbol,
            "amount_wei": self.amount_wei,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionOutcome:
    operation_id: str
    executed_steps: list[str]
    tx_hashes: list[str]
    reconciled: bool
    simulated: bool = False


class RebalanceEngine:
    def __init__(
        self,
        tx_manager: TransactionManager,
        portfolio_manager: PortfolioManager,
        reconciliation_service: ReconciliationService,
        approval_manager: ApprovalManager,
        journal: ExecutionJournal,
        protocols: dict[str, BaseProtocol],
        paper_portfolio: PaperPortfolioLedger | None = None,
    ):
        self.tx_manager = tx_manager
        self.portfolio_manager = portfolio_manager
        self.reconciliation_service = reconciliation_service
        self.approval_manager = approval_manager
        self.journal = journal
        self.protocols = protocols
        self.paper_portfolio = paper_portfolio
        self.logger = logging.getLogger("yield-optimizer-bot.services.rebalance_engine")

    async def _simulate_plan(self, tx: dict[str, Any], description: str) -> None:
        plan = TxPlan(tx=tx, description=description)
        pending_nonce = int(self.tx_manager.w3.eth.get_transaction_count(self.tx_manager.account.address, "pending"))
        await asyncio.to_thread(
            self.tx_manager.dry_run_simulate,
            self.tx_manager.build_and_validate(plan, forced_nonce=pending_nonce).tx,
        )

    async def execute(self, plan: RebalancePlan) -> ExecutionOutcome:
        if plan.profitability and not plan.profitability.is_profitable:
            raise ValueError(f"Rebalance blocked by profitability engine: {plan.profitability.reasons}")

        deposit_protocol = self.protocols[plan.deposit_protocol]
        withdraw_protocol = self.protocols.get(plan.withdraw_protocol) if plan.withdraw_protocol else None
        asset_symbol = plan.asset_symbol

        journal_entry = self.journal.start_operation(
            operation_type="rebalance",
            idempotency_key=plan.idempotency_key,
            metadata={
                "asset_symbol": asset_symbol,
                "amount_wei": plan.amount_wei,
                "withdraw_protocol": plan.withdraw_protocol,
                "deposit_protocol": plan.deposit_protocol,
                **plan.metadata,
            },
        )
        self.portfolio_manager.set_pending_operation(journal_entry.operation_id)
        if self.paper_portfolio is not None:
            self.paper_portfolio.set_pending_operation(journal_entry.operation_id)
        self.journal.append_step(journal_entry.operation_id, step="started", status="running", payload=journal_entry.metadata)

        pre_snapshot = await self.reconciliation_service.reconcile([asset_symbol])
        self.journal.append_step(
            journal_entry.operation_id,
            step="reconciled_pre_execution",
            status="running",
            payload={"inconsistencies": pre_snapshot.inconsistencies},
        )

        deposit_health = await deposit_protocol.check_health(asset_symbol)
        if not deposit_health.is_healthy or deposit_health.is_paused:
            self.journal.mark_failed(journal_entry.operation_id, "deposit_protocol_unhealthy")
            raise RuntimeError(f"Deposit protocol unhealthy: {deposit_protocol.protocol_name}")

        withdraw_position = None
        if withdraw_protocol is not None:
            withdraw_health = await withdraw_protocol.check_health(asset_symbol)
            if not withdraw_health.is_healthy:
                self.journal.mark_failed(journal_entry.operation_id, "withdraw_protocol_unhealthy")
                raise RuntimeError(f"Withdraw protocol unhealthy: {withdraw_protocol.protocol_name}")
            if self.paper_portfolio is not None:
                withdraw_amount = self.paper_portfolio.get_protocol_balance(withdraw_protocol.protocol_name, asset_symbol)
                withdraw_position = PositionSnapshot(
                    protocol=withdraw_protocol.protocol_name,
                    chain=withdraw_protocol.chain,
                    asset_symbol=asset_symbol,
                    wallet_balance_wei=self.paper_portfolio.get_wallet_balance(asset_symbol),
                    supplied_balance_wei=withdraw_amount,
                    withdrawable_balance_wei=withdraw_amount,
                    allowance_wei=2**256 - 1,
                    shares_balance_wei=withdraw_amount,
                    details={"paper_trading": True},
                )
            else:
                withdraw_position = await withdraw_protocol.discover_position(self.tx_manager.account.address, asset_symbol)
            if withdraw_position.withdrawable_balance_wei < plan.amount_wei:
                self.journal.mark_failed(journal_entry.operation_id, "insufficient_withdrawable_balance")
                raise RuntimeError("Insufficient protocol position for withdraw step")

        if self.paper_portfolio is not None:
            deposit_amount = self.paper_portfolio.get_protocol_balance(deposit_protocol.protocol_name, asset_symbol)
            deposit_position = PositionSnapshot(
                protocol=deposit_protocol.protocol_name,
                chain=deposit_protocol.chain,
                asset_symbol=asset_symbol,
                wallet_balance_wei=self.paper_portfolio.get_wallet_balance(asset_symbol),
                supplied_balance_wei=deposit_amount,
                withdrawable_balance_wei=deposit_amount,
                allowance_wei=2**256 - 1,
                shares_balance_wei=deposit_amount,
                details={"paper_trading": True},
            )
        else:
            deposit_position = await deposit_protocol.discover_position(self.tx_manager.account.address, asset_symbol)
        if withdraw_protocol is None and deposit_position.wallet_balance_wei < plan.amount_wei:
            self.journal.mark_failed(journal_entry.operation_id, "insufficient_wallet_balance")
            raise RuntimeError("Insufficient wallet balance for direct deposit")

        if self.paper_portfolio is not None:
            approval_check = type("PaperApprovalCheck", (), {
                "token_address": deposit_protocol.get_asset_address(asset_symbol),
                "spender": deposit_protocol.get_spender(asset_symbol),
                "current_allowance_wei": 2**256 - 1,
                "required_allowance_wei": plan.amount_wei,
                "approval_required": False,
                "approval_amount_wei": 0,
            })()
        else:
            approval_check = await self.approval_manager.check_allowance(
                token_address=deposit_protocol.get_asset_address(asset_symbol),
                spender=deposit_protocol.get_spender(asset_symbol),
                required_allowance_wei=plan.amount_wei,
            )

        preview_withdraw = None if withdraw_protocol is None else await withdraw_protocol.preview_withdraw(asset_symbol, plan.amount_wei)
        preview_deposit = await deposit_protocol.preview_deposit(asset_symbol, plan.amount_wei)

        precheck_payload = {
            "approval_required": approval_check.approval_required,
            "current_allowance_wei": approval_check.current_allowance_wei,
            "preview_deposit": preview_deposit.details,
            "preview_withdraw": None if preview_withdraw is None else preview_withdraw.details,
        }
        self.journal.append_step(journal_entry.operation_id, step="prechecks_completed", status="running", payload=precheck_payload)

        approval_tx = None
        if approval_check.approval_required:
            approval_tx = self.approval_manager.build_approval_tx(
                token_address=approval_check.token_address,
                spender=approval_check.spender,
                amount_wei=approval_check.approval_amount_wei,
            )
            await self._simulate_plan(approval_tx, "approval")

        withdraw_tx = None
        if withdraw_protocol is not None:
            withdraw_tx = withdraw_protocol.build_withdraw_plan(asset_symbol, plan.amount_wei)
            await self._simulate_plan(withdraw_tx, "withdraw")

        deposit_tx = deposit_protocol.build_deposit_plan(asset_symbol, plan.amount_wei)
        if withdraw_protocol is None:
            await self._simulate_plan(deposit_tx, "deposit")
        else:
            self.journal.append_step(
                journal_entry.operation_id,
                step="deposit_simulation_deferred",
                status="running",
                payload={"reason": "deposit depends on prior withdraw state transition"},
            )

        tx_hashes: list[str] = []
        executed_steps: list[str] = []

        try:
            if approval_tx is not None:
                sent = await asyncio.to_thread(self.tx_manager.execute_plan, TxPlan(tx=approval_tx, description="approval"), BroadcastPolicy())
                if sent.tx_hash:
                    tx_hashes.append(sent.tx_hash)
                    self.journal.record_broadcast(journal_entry.operation_id, sent.tx_hash, sent.nonce, sent.receipt)
                executed_steps.append("approval")
                self.journal.append_step(
                    journal_entry.operation_id,
                    step="approval_executed",
                    status="running",
                    payload={"simulated": sent.simulated, "gas_estimate": sent.gas_estimate, "tx_hash": sent.tx_hash},
                )

            if withdraw_tx is not None:
                sent = await asyncio.to_thread(self.tx_manager.execute_plan, TxPlan(tx=withdraw_tx, description="withdraw"), BroadcastPolicy())
                if sent.tx_hash:
                    tx_hashes.append(sent.tx_hash)
                    self.journal.record_broadcast(journal_entry.operation_id, sent.tx_hash, sent.nonce, sent.receipt)
                executed_steps.append("withdraw")
                self.journal.append_step(
                    journal_entry.operation_id,
                    step="withdraw_executed",
                    status="running",
                    payload={"simulated": sent.simulated, "gas_estimate": sent.gas_estimate, "tx_hash": sent.tx_hash},
                )

            sent = await asyncio.to_thread(self.tx_manager.execute_plan, TxPlan(tx=deposit_tx, description="deposit"), BroadcastPolicy())
            if sent.tx_hash:
                tx_hashes.append(sent.tx_hash)
                self.journal.record_broadcast(journal_entry.operation_id, sent.tx_hash, sent.nonce, sent.receipt)
            executed_steps.append("deposit")
            self.journal.append_step(
                journal_entry.operation_id,
                step="deposit_executed",
                status="running",
                payload={"simulated": sent.simulated, "gas_estimate": sent.gas_estimate, "tx_hash": sent.tx_hash},
            )

            if self.paper_portfolio is not None:
                gas_cost_usd = float(plan.metadata.get("gas_cost_usd", 0.0))
                slippage_cost_usd = float(plan.metadata.get("slippage_cost_usd", 0.0))
                candidate_net_apy = float(plan.metadata.get("candidate_net_apy", 0.0))
                self.paper_portfolio.apply_simulated_rebalance(
                    asset_symbol=asset_symbol,
                    amount_wei=plan.amount_wei,
                    withdraw_protocol=withdraw_protocol.protocol_name if withdraw_protocol is not None else None,
                    deposit_protocol=deposit_protocol.protocol_name,
                    expected_shares_wei=preview_deposit.expected_shares_wei,
                    gas_cost_usd=gas_cost_usd,
                    slippage_cost_usd=slippage_cost_usd,
                    apy=candidate_net_apy,
                )

            post_snapshot = await self.reconciliation_service.reconcile([asset_symbol])
            post_position = post_snapshot.snapshot.positions.get(plan.deposit_protocol, {}).get(asset_symbol)
            if post_position is None or post_position.supplied_balance_wei <= deposit_position.supplied_balance_wei:
                raise RuntimeError("Post-execution reconciliation did not confirm increased destination position")

            self.journal.mark_completed(
                journal_entry.operation_id,
                payload={
                    "tx_hashes": tx_hashes,
                    "executed_steps": executed_steps,
                    "post_inconsistencies": post_snapshot.inconsistencies,
                },
            )
            if self.paper_portfolio is None:
                self.portfolio_manager.touch_rebalance(plan.deposit_protocol)
            else:
                self.portfolio_manager.set_pending_operation(None)
                self.paper_portfolio.set_pending_operation(None)
            return ExecutionOutcome(
                operation_id=journal_entry.operation_id,
                executed_steps=executed_steps,
                tx_hashes=tx_hashes,
                reconciled=True,
                simulated=sent.simulated,
            )
        except Exception as exc:  # noqa: BLE001
            self.journal.mark_failed(journal_entry.operation_id, str(exc), payload={"executed_steps": executed_steps, "tx_hashes": tx_hashes})
            self.portfolio_manager.set_pending_operation(journal_entry.operation_id)
            if self.paper_portfolio is not None:
                self.paper_portfolio.set_pending_operation(journal_entry.operation_id)
            raise
