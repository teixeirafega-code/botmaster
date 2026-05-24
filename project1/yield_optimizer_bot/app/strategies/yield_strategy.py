from __future__ import annotations

import statistics
from dataclasses import dataclass

from app.services.profitability_engine import ProfitabilityReport
from app.utils.helpers import bps_to_decimal


@dataclass(frozen=True)
class YieldObservation:
    protocol: str
    asset_symbol: str
    apy: float
    ts: float


class YieldStrategy:
    def __init__(
        self,
        min_apy_diff_bps: int,
        cooldown_seconds: int,
        min_profit_usd: float,
        slippage_bps: int,
        moving_average_window: int = 5,
        min_persistence_seconds: int = 600,
        max_apy_volatility: float = 0.05,
    ):
        self.min_apy_diff_bps = min_apy_diff_bps
        self.cooldown_seconds = cooldown_seconds
        self.min_profit_usd = min_profit_usd
        self.slippage_bps = slippage_bps
        self.moving_average_window = moving_average_window
        self.min_persistence_seconds = min_persistence_seconds
        self.max_apy_volatility = max_apy_volatility
        self._history: dict[tuple[str, str], list[YieldObservation]] = {}

    def record_observation(self, protocol: str, asset_symbol: str, apy: float, ts: float) -> None:
        key = (protocol, asset_symbol)
        history = list(self._history.get(key, []))
        history.append(YieldObservation(protocol=protocol, asset_symbol=asset_symbol, apy=float(apy), ts=float(ts)))
        self._history[key] = history[-200:]

    def load_history(self, asset_symbol: str, observations: list[dict]) -> None:
        for item in observations:
            protocol = item.get("protocol")
            apy = item.get("apy")
            ts = item.get("ts")
            if protocol is None or apy is None or ts is None:
                continue
            self.record_observation(protocol=protocol, asset_symbol=asset_symbol, apy=float(apy), ts=float(ts))

    def export_asset_history(self, asset_symbol: str) -> list[dict]:
        exported: list[dict] = []
        for (protocol, observed_asset), observations in self._history.items():
            if observed_asset != asset_symbol:
                continue
            for observation in observations:
                exported.append(
                    {
                        "protocol": protocol,
                        "asset_symbol": observed_asset,
                        "apy": observation.apy,
                        "ts": observation.ts,
                    }
                )
        return sorted(exported, key=lambda item: item["ts"])

    def _recent_apys(self, protocol: str, asset_symbol: str) -> list[float]:
        history = self._history.get((protocol, asset_symbol), [])
        return [item.apy for item in history[-self.moving_average_window :]]

    def smoothed_apy(self, protocol: str, asset_symbol: str, fallback_apy: float) -> float:
        values = self._recent_apys(protocol, asset_symbol)
        if not values:
            return float(fallback_apy)
        return float(sum(values) / len(values))

    def volatility(self, protocol: str, asset_symbol: str) -> float:
        values = self._recent_apys(protocol, asset_symbol)
        if len(values) < 2:
            return 0.0
        return float(statistics.pstdev(values))

    def dynamic_threshold(self, protocol: str, asset_symbol: str) -> float:
        base_threshold = float(bps_to_decimal(self.min_apy_diff_bps))
        volatility_penalty = self.volatility(protocol, asset_symbol) * 1.5
        return base_threshold + volatility_penalty

    def persistence_satisfied(self, protocol: str, asset_symbol: str, now_ts: float) -> bool:
        history = self._history.get((protocol, asset_symbol), [])
        if not history:
            return False
        newest = history[-1]
        oldest_allowed_ts = now_ts - self.min_persistence_seconds
        persistent = [item for item in history if item.ts >= oldest_allowed_ts]
        return bool(persistent) and persistent[0].ts <= oldest_allowed_ts + 1

    def should_rebalance(
        self,
        current_protocol: str | None,
        last_rebalance_ts: float,
        candidate_protocol: str,
        candidate_net_apy: float,
        current_net_apy: float,
        estimated_gas_fee_usd: float,
        cooldown_now: float,
        profitability: ProfitabilityReport | None = None,
        asset_symbol: str = "USDC",
    ) -> tuple[bool, str]:
        if cooldown_now - last_rebalance_ts < self.cooldown_seconds:
            return False, "cooldown_active"

        if current_protocol == candidate_protocol:
            return False, "already_on_best_protocol"

        if self.volatility(candidate_protocol, asset_symbol) > self.max_apy_volatility:
            return False, "apy_volatility_too_high"

        if not self.persistence_satisfied(candidate_protocol, asset_symbol, cooldown_now):
            return False, "candidate_apy_not_persistent"

        smoothed_candidate = self.smoothed_apy(candidate_protocol, asset_symbol, candidate_net_apy)
        smoothed_current = current_net_apy
        if current_protocol:
            smoothed_current = self.smoothed_apy(current_protocol, asset_symbol, current_net_apy)

        apy_diff = smoothed_candidate - smoothed_current
        if apy_diff <= self.dynamic_threshold(candidate_protocol, asset_symbol):
            return False, "apy_diff_below_dynamic_threshold"

        if profitability is not None:
            if not profitability.is_profitable:
                return False, "profitability_engine_rejected"
            if profitability.expected_profit_usd < self.min_profit_usd:
                return False, "profit_below_min_profit_usd"

        if estimated_gas_fee_usd <= 0:
            return False, "invalid_gas_estimate"

        return True, "ok"
