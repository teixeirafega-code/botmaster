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

