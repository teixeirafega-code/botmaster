from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskDecision:
    allow: bool
    reason: str


class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._window_start = time.time()
        self._count = 0

    def allow(self) -> bool:
        now = time.time()
        if now - self._window_start >= 60:
            self._window_start = now
            self._count = 0
        if self._count >= self.max_per_minute:
            return False
        self._count += 1
        return True


class RiskManager:
    def __init__(self, emergency_stop_path: str, max_consecutive_failures: int, rate_limit_per_minute: int):
        self.emergency_stop_path = emergency_stop_path
        self.max_consecutive_failures = max_consecutive_failures
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self._consecutive_failures = 0

    def emergency_stop(self) -> bool:
        return os.path.exists(self.emergency_stop_path)

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def decision(
        self,
        gas_congestion_level: float,
        gas_volatility_bps: int,
        allow_operations: bool,
        *,
        protocol_paused: bool = False,
        oracle_sane: bool = True,
        abnormal_apy: bool = False,
        exposure_fraction: float = 0.0,
        max_protocol_exposure_fraction: float = 1.0,
        confidence_score: float = 1.0,
        min_confidence_score: float = 0.4,
    ) -> RiskDecision:
        if self.emergency_stop():
            return RiskDecision(False, "emergency_stop_triggered")
        if self._consecutive_failures >= self.max_consecutive_failures:
            return RiskDecision(False, "max_consecutive_failures_exceeded")
        if not allow_operations:
            return RiskDecision(False, "allow_operations_false")
        if protocol_paused:
            return RiskDecision(False, "protocol_paused")
        if not oracle_sane:
            return RiskDecision(False, "oracle_sanity_failed")
        if abnormal_apy:
            return RiskDecision(False, "abnormal_apy_detected")
        if exposure_fraction > max_protocol_exposure_fraction:
            return RiskDecision(False, "protocol_exposure_limit_exceeded")
        if confidence_score < min_confidence_score:
            return RiskDecision(False, "confidence_score_too_low")
        if gas_congestion_level >= 0.85:
            return RiskDecision(False, "high_gas_congestion")
        if gas_volatility_bps > 0:
            pass
        if not self.rate_limiter.allow():
            return RiskDecision(False, "rate_limited")
        return RiskDecision(True, "ok")
