from __future__ import annotations

from app.services.profitability_engine import ProfitabilityEngine, ProfitabilityInputs


def test_profitability_engine_rejects_negative_expected_profit():
    engine = ProfitabilityEngine()
    report = engine.evaluate(
        ProfitabilityInputs(
            capital_usd=1_000,
            candidate_apy=0.05,
            current_apy=0.04,
            gas_cost_usd=25,
            slippage_cost_usd=5,
            expected_holding_days=7,
            protocol_risk_score=0.3,
            liquidity_depth_score=0.8,
        )
    )

    assert report.is_profitable is False
    assert "expected_profit_non_positive" in report.reasons


def test_profitability_engine_accepts_positive_net_transition():
    engine = ProfitabilityEngine()
    report = engine.evaluate(
        ProfitabilityInputs(
            capital_usd=500_000,
            candidate_apy=0.09,
            current_apy=0.04,
            gas_cost_usd=12,
            slippage_cost_usd=20,
            expected_holding_days=60,
            protocol_risk_score=0.1,
            liquidity_depth_score=0.95,
        )
    )

    assert report.is_profitable is True
    assert report.expected_profit_usd > 0
    assert report.payback_days < 60
