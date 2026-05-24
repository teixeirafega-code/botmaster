from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PaperPosition:
    protocol: str
    asset_symbol: str
    amount_wei: int
    avg_entry_apy: float = 0.0
    opened_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class PaperAnalytics:
    simulated_gas_spent_usd: float = 0.0
    simulated_slippage_spent_usd: float = 0.0
    hypothetical_pnl_usd: float = 0.0
    avoided_bad_trades: int = 0
    rebalance_count: int = 0
    last_yield_accrual_ts: float = 0.0
    realized_simulated_yield_usd: float = 0.0
    decision_rejections: list[dict[str, Any]] = field(default_factory=list)
    protocol_rank_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PaperState:
    wallet_balances: dict[str, int] = field(default_factory=dict)
    protocol_positions: dict[str, dict[str, PaperPosition]] = field(default_factory=dict)
    analytics: PaperAnalytics = field(default_factory=PaperAnalytics)
    last_rebalance_ts: float = 0.0
    current_protocol: str | None = None
    pending_operation_id: str | None = None


class PaperPortfolioLedger:
    def __init__(self, state_path: str, initial_holdings: dict[str, int] | None = None):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_holdings = {k: int(v) for k, v in (initial_holdings or {}).items()}
        self.state = self._load_or_seed()

    def _load_or_seed(self) -> PaperState:
        if self.state_path.exists():
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return self._deserialize(payload)
        seed = PaperState(wallet_balances=dict(self.initial_holdings))
        seed.analytics.last_yield_accrual_ts = time.time()
        self._persist(seed)
        return seed

    def _deserialize(self, payload: dict[str, Any]) -> PaperState:
        protocol_positions: dict[str, dict[str, PaperPosition]] = {}
        for protocol, positions in (payload.get("protocol_positions") or {}).items():
            protocol_positions[protocol] = {}
            for asset_symbol, position in positions.items():
                protocol_positions[protocol][asset_symbol] = PaperPosition(**position)
        analytics = PaperAnalytics(**(payload.get("analytics") or {}))
        return PaperState(
            wallet_balances={k: int(v) for k, v in (payload.get("wallet_balances") or {}).items()},
            protocol_positions=protocol_positions,
            analytics=analytics,
            last_rebalance_ts=float(payload.get("last_rebalance_ts", 0.0)),
            current_protocol=payload.get("current_protocol"),
            pending_operation_id=payload.get("pending_operation_id"),
        )

    def _serialize(self, state: PaperState) -> dict[str, Any]:
        protocol_positions: dict[str, dict[str, Any]] = {}
        for protocol, positions in state.protocol_positions.items():
            protocol_positions[protocol] = {}
            for asset_symbol, position in positions.items():
                protocol_positions[protocol][asset_symbol] = asdict(position)
        return {
            "wallet_balances": state.wallet_balances,
            "protocol_positions": protocol_positions,
            "analytics": asdict(state.analytics),
            "last_rebalance_ts": state.last_rebalance_ts,
            "current_protocol": state.current_protocol,
            "pending_operation_id": state.pending_operation_id,
        }

    def _persist(self, state: PaperState) -> None:
        self.state_path.write_text(json.dumps(self._serialize(state), indent=2, sort_keys=True), encoding="utf-8")

    def set_pending_operation(self, operation_id: str | None) -> None:
        self.state.pending_operation_id = operation_id
        self._persist(self.state)

    def get_wallet_balance(self, asset_symbol: str) -> int:
        return int(self.state.wallet_balances.get(asset_symbol, 0))

    def get_protocol_balance(self, protocol: str, asset_symbol: str) -> int:
        return int(self.state.protocol_positions.get(protocol, {}).get(asset_symbol, PaperPosition(protocol, asset_symbol, 0)).amount_wei)

    def total_balance(self, asset_symbol: str) -> int:
        total = self.get_wallet_balance(asset_symbol)
        for positions in self.state.protocol_positions.values():
            total += int(positions.get(asset_symbol, PaperPosition("", asset_symbol, 0)).amount_wei)
        return total

    def dominant_protocol(self, asset_symbol: str) -> str | None:
        winner = None
        winner_balance = -1
        for protocol, positions in self.state.protocol_positions.items():
            balance = int(positions.get(asset_symbol, PaperPosition(protocol, asset_symbol, 0)).amount_wei)
            if balance > winner_balance:
                winner_balance = balance
                winner = protocol if balance > 0 else None
        return winner

    def apply_yield(self, asset_symbol: str, protocol_apys: dict[str, float], now_ts: float | None = None) -> float:
        now = now_ts or time.time()
        last_ts = self.state.analytics.last_yield_accrual_ts or now
        elapsed_seconds = max(0.0, now - last_ts)
        accrued_total_usd = 0.0
        if elapsed_seconds == 0:
            return 0.0
        year_fraction = elapsed_seconds / (365 * 24 * 60 * 60)
        for protocol, positions in self.state.protocol_positions.items():
            position = positions.get(asset_symbol)
            if not position or position.amount_wei <= 0:
                continue
            apy = float(protocol_apys.get(protocol, position.avg_entry_apy))
            yield_wei = int(position.amount_wei * apy * year_fraction)
            position.amount_wei += yield_wei
            position.updated_at = now
            accrued_total_usd += yield_wei / 10**6
        self.state.analytics.realized_simulated_yield_usd += accrued_total_usd
        self.state.analytics.hypothetical_pnl_usd += accrued_total_usd
        self.state.analytics.last_yield_accrual_ts = now
        self._persist(self.state)
        return accrued_total_usd

    def record_rejection(self, reason: str, payload: dict[str, Any]) -> None:
        self.state.analytics.avoided_bad_trades += 1
        self.state.analytics.decision_rejections.append({"ts": time.time(), "reason": reason, "payload": payload})
        self.state.analytics.decision_rejections = self.state.analytics.decision_rejections[-500:]
        self._persist(self.state)

    def record_protocol_ranking(self, asset_symbol: str, ranking: list[dict[str, Any]]) -> None:
        self.state.analytics.protocol_rank_history.append({"ts": time.time(), "asset_symbol": asset_symbol, "ranking": ranking})
        self.state.analytics.protocol_rank_history = self.state.analytics.protocol_rank_history[-500:]
        self._persist(self.state)

    def apply_simulated_rebalance(
        self,
        *,
        asset_symbol: str,
        amount_wei: int,
        withdraw_protocol: str | None,
        deposit_protocol: str,
        expected_shares_wei: int,
        gas_cost_usd: float,
        slippage_cost_usd: float,
        apy: float,
    ) -> None:
        now = time.time()
        if withdraw_protocol:
            position = self.state.protocol_positions.setdefault(withdraw_protocol, {}).setdefault(
                asset_symbol,
                PaperPosition(protocol=withdraw_protocol, asset_symbol=asset_symbol, amount_wei=0),
            )
            if position.amount_wei < amount_wei:
                raise RuntimeError("Paper ledger underflow on withdraw")
            position.amount_wei -= amount_wei
            position.updated_at = now
            self.state.wallet_balances[asset_symbol] = self.get_wallet_balance(asset_symbol) + amount_wei

        wallet_balance = self.get_wallet_balance(asset_symbol)
        if wallet_balance < amount_wei:
            raise RuntimeError("Paper ledger has insufficient wallet balance for deposit")
        self.state.wallet_balances[asset_symbol] = wallet_balance - amount_wei

        target = self.state.protocol_positions.setdefault(deposit_protocol, {}).setdefault(
            asset_symbol,
            PaperPosition(protocol=deposit_protocol, asset_symbol=asset_symbol, amount_wei=0),
        )
        target.amount_wei += amount_wei
        target.avg_entry_apy = apy
        target.opened_at = target.opened_at or now
        target.updated_at = now

        self.state.analytics.simulated_gas_spent_usd += gas_cost_usd
        self.state.analytics.simulated_slippage_spent_usd += slippage_cost_usd
        self.state.analytics.hypothetical_pnl_usd -= gas_cost_usd + slippage_cost_usd
        self.state.analytics.rebalance_count += 1
        self.state.last_rebalance_ts = now
        self.state.current_protocol = deposit_protocol
        self.state.pending_operation_id = None
        self._persist(self.state)
