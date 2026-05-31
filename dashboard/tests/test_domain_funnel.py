import os

os.environ.setdefault("BOTMASTER_EMBEDDED_BOTS", "false")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("FLASK_SECRET_KEY", "test")

from dashboard.app import _acquisition_decision_entries, _top_domain_rows


def test_dashboard_never_shows_high_score_without_final_decision_and_reason():
    decisions = _acquisition_decision_entries(
        [
            {
                "domain": "overbudget.com",
                "score": 94,
                "estimated_sale_price": 1200,
                "price": 99,
                "liquidity_grade": "A",
                "sale_probability": 0.62,
                "expected_value": 744,
                "timestamp": "2026-05-30T12:00:00+00:00",
            }
        ]
    )

    rows = _top_domain_rows(decisions, [], 88)

    assert rows[0]["name"] == "overbudget.com"
    assert rows[0]["final_decision"]
    assert rows[0]["final_reason"]
    assert rows[0]["warning"] == "Dominio de score alto bloqueado: decisao_canonica_incompleta"


def test_dashboard_exposes_policy_result_for_high_score_blocked_domain():
    decisions = _acquisition_decision_entries(
        [
            {
                "domain": "budgetblocked.com",
                "score": 95,
                "estimated_sale_price": 2000,
                "price": 40,
                "extension": ".com",
                "trademark_risk": False,
                "liquidity_grade": "A",
                "sale_probability": 0.7,
                "expected_holding_months": 4,
                "expected_value": 1400,
                "passed_score_filter": True,
                "passed_trademark_filter": True,
                "passed_liquidity_filter": True,
                "passed_extension_filter": True,
                "passed_price_filter": True,
                "passed_budget_filter": False,
                "final_decision": "rejected",
                "final_reason": "max_daily_spend_reached",
                "timestamp": "2026-05-30T12:00:00+00:00",
            }
        ]
    )

    rows = _top_domain_rows(decisions, [], 88)

    assert rows[0]["final_decision"] == "rejected"
    assert rows[0]["final_reason"] == "max_daily_spend_reached"
    assert rows[0]["reached_pending_approval"] is False
    assert rows[0]["warning"] == "Dominio de score alto bloqueado: max_daily_spend_reached"
