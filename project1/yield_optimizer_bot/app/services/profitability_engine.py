from __future__ import annotations

from dataclasses import dataclass
from math import inf


@dataclass(frozen=True)
class ProfitabilityInputs:
    capital_usd: float
    candidate_apy: float
    current_apy: float
    gas_cost_usd: float
    slippage_cost_usd: float
    expected_holding_days: float
    protocol_risk_score: float
    liquidity_depth_score: float


@dataclass(frozen=True)
class ProfitabilityReport:
    is_profitable: bool
    expected_profit_usd: float
    annualized_net_profit_usd: float
    payback_days: float
    break_even_apy_delta: float
    min_profitable_capital_usd: float
    gas_adjusted_apy_delta: float
    confidence_score: float
    reasons: list[str]


class ProfitabilityEngine:
    def evaluate(self, inputs: ProfitabilityInputs) -> ProfitabilityReport:
        reasons: list[str] = []
        apy_delta = float(inputs.candidate_apy) - float(inputs.current_apy)
        risk_penalty = max(0.0, inputs.protocol_risk_score) * 0.02
        liquidity_penalty = max(0.0, 1 - inputs.liquidity_depth_score) * 0.01
        gas_adjusted_apy_delta = apy_delta - risk_penalty - liquidity_penalty

        holding_fraction = max(inputs.expected_holding_days, 0.0) / 365.0
        annualized_net_profit_usd = inputs.capital_usd * gas_adjusted_apy_delta
        expected_profit_usd = (annualized_net_profit_usd * holding_fraction) - inputs.gas_cost_usd - inputs.slippage_cost_usd

        total_transition_costs = inputs.gas_cost_usd + inputs.slippage_cost_usd
        if annualized_net_profit_usd <= 0:
            payback_days = inf
        else:
            payback_days = (total_transition_costs / annualized_net_profit_usd) * 365.0

        if inputs.capital_usd <= 0 or inputs.expected_holding_days <= 0:
            break_even_apy_delta = inf
            min_profitable_capital_usd = inf
        else:
            break_even_apy_delta = total_transition_costs / (inputs.capital_usd * holding_fraction)
            min_profitable_capital_usd = total_transition_costs / max(gas_adjusted_apy_delta * holding_fraction, 1e-12)

        if gas_adjusted_apy_delta <= 0:
            reasons.append("gas_adjusted_apy_delta_non_positive")
        if expected_profit_usd <= 0:
            reasons.append("expected_profit_non_positive")
        if payback_days > inputs.expected_holding_days:
            reasons.append("payback_exceeds_holding_window")

        confidence_score = max(0.0, min(1.0, (1 - inputs.protocol_risk_score) * inputs.liquidity_depth_score))
        return ProfitabilityReport(
            is_profitable=expected_profit_usd > 0 and gas_adjusted_apy_delta > 0 and payback_days <= max(inputs.expected_holding_days, 0.0001),
            expected_profit_usd=expected_profit_usd,
            annualized_net_profit_usd=annualized_net_profit_usd,
            payback_days=payback_days,
            break_even_apy_delta=break_even_apy_delta,
            min_profitable_capital_usd=min_profitable_capital_usd,
            gas_adjusted_apy_delta=gas_adjusted_apy_delta,
            confidence_score=confidence_score,
            reasons=reasons or ["profitable"],
        )
