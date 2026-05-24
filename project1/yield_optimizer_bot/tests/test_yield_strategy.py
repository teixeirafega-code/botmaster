from __future__ import annotations

from app.services.profitability_engine import ProfitabilityReport
from app.strategies.yield_strategy import YieldStrategy


def _profitable() -> ProfitabilityReport:
    return ProfitabilityReport(
        is_profitable=True,
        expected_profit_usd=50,
        annualized_net_profit_usd=500,
        payback_days=10,
        break_even_apy_delta=0.01,
        min_profitable_capital_usd=1000,
        gas_adjusted_apy_delta=0.03,
        confidence_score=0.9,
        reasons=["profitable"],
    )


def test_yield_strategy_requires_persistence_and_allows_profitable_move():
    strategy = YieldStrategy(
        min_apy_diff_bps=50,
        cooldown_seconds=60,
        min_profit_usd=10,
        slippage_bps=20,
        moving_average_window=3,
        min_persistence_seconds=120,
        max_apy_volatility=0.10,
    )

    strategy.record_observation("aave", "USDC", 0.04, 0)
    strategy.record_observation("aave", "USDC", 0.041, 60)
    strategy.record_observation("aave", "USDC", 0.042, 180)
    strategy.record_observation("compound", "USDC", 0.07, 0)
    strategy.record_observation("compound", "USDC", 0.071, 60)
    strategy.record_observation("compound", "USDC", 0.072, 180)

    allow, reason = strategy.should_rebalance(
        current_protocol="aave",
        last_rebalance_ts=0,
        candidate_protocol="compound",
        candidate_net_apy=0.072,
        current_net_apy=0.042,
        estimated_gas_fee_usd=5,
        cooldown_now=180,
        profitability=_profitable(),
        asset_symbol="USDC",
    )

    assert allow is True
    assert reason == "ok"


def test_yield_strategy_rejects_high_volatility_candidate():
    strategy = YieldStrategy(
        min_apy_diff_bps=50,
        cooldown_seconds=0,
        min_profit_usd=10,
        slippage_bps=20,
        moving_average_window=5,
        min_persistence_seconds=1,
        max_apy_volatility=0.01,
    )

    for ts, apy in enumerate([0.02, 0.15, 0.01, 0.18, 0.02], start=1):
        strategy.record_observation("curve", "USDC", apy, ts)

    allow, reason = strategy.should_rebalance(
        current_protocol="aave",
        last_rebalance_ts=0,
        candidate_protocol="curve",
        candidate_net_apy=0.18,
        current_net_apy=0.03,
        estimated_gas_fee_usd=5,
        cooldown_now=10,
        profitability=_profitable(),
        asset_symbol="USDC",
    )

    assert allow is False
    assert reason == "apy_volatility_too_high"
