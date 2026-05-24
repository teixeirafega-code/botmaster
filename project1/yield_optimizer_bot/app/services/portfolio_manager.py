from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PortfolioState:
    last_rebalance_ts: float = 0.0
    current_protocol: str | None = None
    holdings: dict[str, int] = field(default_factory=dict)
    last_onchain_sync_ts: float = 0.0
    last_seen_positions: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    pending_operation_id: str | None = None
    apy_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class PortfolioManager:
    def __init__(self, state_path: str):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state: PortfolioState = self._load_or_seed()

    def _load_or_seed(self) -> PortfolioState:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return PortfolioState(
                last_rebalance_ts=float(data.get("last_rebalance_ts", 0.0)),
                current_protocol=data.get("current_protocol"),
                holdings={k: int(v) for k, v in (data.get("holdings") or {}).items()},
                last_onchain_sync_ts=float(data.get("last_onchain_sync_ts", 0.0)),
                last_seen_positions=data.get("last_seen_positions") or {},
                pending_operation_id=data.get("pending_operation_id"),
                apy_history=data.get("apy_history") or {},
            )
        seed = PortfolioState()
        self._persist(seed)
        return seed

    def _persist(self, state: PortfolioState) -> None:
        self.state_path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")

    def update_from_snapshot(self, snapshot) -> None:
        serialized_positions: dict[str, dict[str, dict[str, Any]]] = {}
        for protocol, assets in snapshot.positions.items():
            serialized_positions[protocol] = {}
            for asset_symbol, position in assets.items():
                serialized_positions[protocol][asset_symbol] = {
                    "wallet_balance_wei": int(position.wallet_balance_wei),
                    "supplied_balance_wei": int(position.supplied_balance_wei),
                    "withdrawable_balance_wei": int(position.withdrawable_balance_wei),
                    "allowance_wei": int(position.allowance_wei),
                    "shares_balance_wei": int(position.shares_balance_wei),
                    "details": position.details,
                }

        self.state.holdings = {asset_symbol: int(amount) for asset_symbol, amount in snapshot.totals_by_asset.items()}
        self.state.current_protocol = snapshot.dominant_protocol_by_asset.get(next(iter(snapshot.totals_by_asset.keys())), None)
        self.state.last_onchain_sync_ts = time.time()
        self.state.last_seen_positions = serialized_positions
        self._persist(self.state)

    def set_pending_operation(self, operation_id: str | None) -> None:
        self.state.pending_operation_id = operation_id
        self._persist(self.state)

    def record_apy_observation(self, asset_symbol: str, observations: list[dict[str, Any]], max_points: int = 200) -> None:
        history = list(self.state.apy_history.get(asset_symbol, []))
        history.extend(observations)
        self.state.apy_history[asset_symbol] = history[-max_points:]
        self._persist(self.state)

    def touch_rebalance(self, new_protocol: str) -> None:
        self.state.last_rebalance_ts = time.time()
        self.state.current_protocol = new_protocol
        self.state.pending_operation_id = None
        self._persist(self.state)
