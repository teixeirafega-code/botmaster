from app.models import DomainStatus, ManagedDomain
from app.services.profit_tracker import ProfitTracker


def test_profit_snapshot():
    domains = [
        ManagedDomain(name="one.com", source="test", status=DomainStatus.LISTED, score=75, acquisition_cost=12),
        ManagedDomain(name="two.com", source="test", status=DomainStatus.SOLD, score=91, acquisition_cost=12, sale_price=500),
    ]
    snapshot = ProfitTracker().snapshot(domains)
    assert snapshot["domains_monitored"] == 2
    assert snapshot["registered"] == 2
    assert snapshot["sold"] == 1
    assert snapshot["total_profit"] == 476
    assert snapshot["total_portfolio_value"] == 500


def test_profit_tracker_domain_rows_calculate_roi():
    domains = [
        ManagedDomain(name="sold.com", source="test", status=DomainStatus.SOLD, score=91, acquisition_cost=20, sale_price=220),
    ]

    rows = ProfitTracker().domain_rows(domains)

    assert rows[0]["domain"] == "sold.com"
    assert rows[0]["roi"] == 10
