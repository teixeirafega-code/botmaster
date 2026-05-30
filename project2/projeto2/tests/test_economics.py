import csv
from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import Settings
from app.economics.backtesting import BacktestingEngine, BacktestTrade
from app.economics.capital_allocator import CapitalAllocator
from app.economics.models import ValuationFactors, ValuationResult
from app.economics.pricing import DynamicPricingEngine
from app.economics.roi import ROIOptimizer
from app.economics.valuation_engine import ValuationEngine
from app.models import DomainCandidate, DomainStatus, ManagedDomain


def make_valuation(**overrides):
    factors = ValuationFactors(
        comparable_sales=0.5,
        commercial_intent=0.5,
        cpc_value=0.5,
        search_demand=0.5,
        extension_quality=1,
        linguistic_quality=0.8,
        brandability=0.8,
        length_quality=0.8,
        pronounceability=0.8,
        trend_momentum=0.7,
        seo_authority=0.7,
        backlink_quality=0.5,
        spam_safety=0.9,
        trademark_safety=0.9,
        archive_quality=0.7,
        liquidity_probability=0.5,
    )
    data = {
        "domain": "test.com",
        "score": 75,
        "fair_market_value": 1000,
        "expected_resale_probability": 0.5,
        "estimated_holding_days": 120,
        "expected_sale_price": 500,
        "expected_roi": 10,
        "liquidity_adjusted_roi": 5,
        "time_adjusted_roi": 1,
        "purchase_confidence": 0.8,
        "recommended_purchase_price": 12,
        "recommended_list_price": 1000,
        "niche": "general",
        "extension": ".com",
        "factors": factors,
    }
    data.update(overrides)
    return ValuationResult(**data)


@pytest.mark.asyncio
async def test_valuation_engine_outputs_profitability_metrics():
    valuation = await ValuationEngine(Settings()).value(
        DomainCandidate(
            name="aicloud.com",
            source="test",
            age_years=8,
            backlinks=300,
            google_indexed=True,
            keyword_value=20,
            extension_points=10,
        )
    )

    assert 0 <= valuation.score <= 100
    assert valuation.fair_market_value > 0
    assert 0 < valuation.expected_resale_probability <= 0.95
    assert valuation.recommended_list_price >= 99


def test_roi_optimizer_rejects_low_liquidity_candidate():
    settings = Settings()
    valuation = make_valuation(expected_resale_probability=0.01)
    decision = ROIOptimizer(settings).decide(valuation)
    assert decision.approved is False
    assert decision.reason == "low_resale_probability"


def test_capital_allocator_blocks_extension_concentration():
    settings = Settings()
    allocator = CapitalAllocator(settings)
    portfolio = [
        ManagedDomain(name="one.com", source="test", status=DomainStatus.LISTED, score=80, acquisition_cost=12),
        ManagedDomain(name="two.com", source="test", status=DomainStatus.LISTED, score=80, acquisition_cost=12),
    ]
    valuation = make_valuation(extension=".com", niche="general", recommended_purchase_price=12)

    allowed, reason = allocator.allowed(valuation, portfolio)

    assert allowed is False
    assert reason == "extension_concentration_limit"


def test_backtesting_report_calculates_strategy_metrics():
    report = BacktestingEngine().run(
        [
            BacktestTrade("a.com", 10, 100, 120, 30, True),
            BacktestTrade("b.com", 10, 100, 0, 180, True),
            BacktestTrade("c.com", 10, 100, 0, 180, False),
        ]
    )

    assert report.trades == 2
    assert report.hit_rate == 0.5
    assert report.false_positive_rate == 0.5


def test_backtesting_stress_test_reports_requested_cost_scenarios(tmp_path):
    report = BacktestingEngine().run_stress_test(
        [
            BacktestTrade("a.com", 100, 160, 150, 30, True, transaction_cost=10, period="2026-01"),
            BacktestTrade("b.com", 100, 150, 135, 30, True, transaction_cost=10, period="2026-02"),
            BacktestTrade("c.com", 100, 150, 130, 30, False, transaction_cost=10, period="2026-03"),
        ],
        output_dir=tmp_path,
    )

    assert [scenario.scenario for scenario in report.scenarios] == [
        "base",
        "2x costs",
        "3x costs",
        "slippage 10 points",
        "slippage 15 points",
        "spread 10 points",
        "spread 15 points",
    ]
    assert report.profitable_scenarios == (
        "base",
        "2x costs",
        "3x costs",
        "slippage 10 points",
        "slippage 15 points",
        "spread 10 points",
        "spread 15 points",
    )
    assert report.fragile is False
    by_name = {scenario.scenario: scenario for scenario in report.scenarios}
    assert by_name["slippage 10 points"].net_pnl == 45
    assert by_name["spread 15 points"].net_pnl == 35

    report_text = (tmp_path / "stress_test_report.txt").read_text(encoding="utf-8")
    assert "Profitable scenarios: base, 2x costs, 3x costs, slippage 10 points, slippage 15 points, spread 10 points, spread 15 points" in report_text
    assert "Strategy fragility: ROBUST" in report_text

    with (tmp_path / "stress_test_scenarios.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["scenario"] == "base"
    assert rows[0]["net_pnl"] == "65.000000"
    assert rows[0]["failed_periods"] == "0"


def test_backtesting_stress_test_marks_strategy_fragile_when_modest_costs_remove_profitability(tmp_path):
    report = BacktestingEngine().run_stress_test(
        [
            BacktestTrade("a.com", 100, 120, 106, 30, True, transaction_cost=5, period="2026-01"),
            BacktestTrade("b.com", 100, 120, 106, 30, True, transaction_cost=5, period="2026-02"),
        ],
        output_dir=tmp_path,
    )

    by_name = {scenario.scenario: scenario for scenario in report.scenarios}
    assert by_name["base"].profitable is True
    assert by_name["2x costs"].profitable is False
    assert by_name["2x costs"].failed_periods == 2
    assert by_name["2x costs"].failed_period_names == ("2026-01", "2026-02")
    assert report.fragile is True
    assert "Strategy fragility: FRAGILE" in (tmp_path / "stress_test_report.txt").read_text(encoding="utf-8")


def test_dynamic_pricing_discounts_stale_inventory():
    valuation = make_valuation(recommended_list_price=1000)
    domain = ManagedDomain(
        name="stale.com",
        source="test",
        status=DomainStatus.LISTED,
        score=70,
        registered_at=datetime.now(UTC) - timedelta(days=400),
    )

    assert DynamicPricingEngine().repricing_recommendation(domain, valuation) == 650
