import json
from datetime import UTC, datetime

import pytest

from app.config.settings import Settings
from app.core.events import EventBus
from app.db.postgres import MemoryDomainRepository
from app.economics.models import ValuationFactors, ValuationResult
from app.models import DomainCandidate, DomainStatus, ManagedDomain
from app.services.acquisition_policy import AcquisitionPolicy
from app.services.domain_manager import DomainManager
from app.services.telegram_notifier import TelegramNotifier


def make_settings(tmp_path, **risk_overrides):
    risk = {
        "min_score_to_buy": 70,
        "min_expected_value": 150.0,
        "max_domain_price_usd": 25.0,
        "max_daily_spend_usd": 50.0,
        "max_weekly_spend_usd": 150.0,
        "max_buys_per_day": 2,
        "max_portfolio_domains": 50,
        "cooldown_minutes_between_buys": 0,
        "allow_non_com": False,
    }
    risk.update(risk_overrides)
    return Settings(
        safe_mode=True,
        pending_approvals_file=tmp_path / "pending_approvals.json",
        purchase_attempts_file=tmp_path / "purchase_attempts.json",
        telegram_bot_token=None,
        telegram_chat_id=None,
        risk=risk,
    )


def make_candidate(domain: str, *, score: int = 82, price: float = 12.0) -> DomainCandidate:
    return DomainCandidate(
        name=domain,
        source="test",
        age_years=5,
        backlinks=500,
        google_indexed=True,
        keyword_value=25,
        extension_points=10 if domain.endswith(".com") else 7,
        score=score,
        source_metadata={"price": price},
    )


def make_valuation(domain: str, **overrides) -> ValuationResult:
    factors = ValuationFactors(
        comparable_sales=0.6,
        commercial_intent=0.7,
        cpc_value=0.6,
        search_demand=0.6,
        extension_quality=1.0 if domain.endswith(".com") else 0.5,
        linguistic_quality=0.8,
        brandability=0.8,
        length_quality=0.8,
        pronounceability=0.8,
        trend_momentum=0.7,
        seo_authority=0.7,
        backlink_quality=0.6,
        spam_safety=0.95,
        trademark_safety=0.95,
        archive_quality=0.7,
        liquidity_probability=0.55,
    )
    data = {
        "domain": domain,
        "score": 82,
        "fair_market_value": 1000.0,
        "expected_resale_probability": 0.55,
        "estimated_holding_days": 150,
        "expected_sale_price": 550.0,
        "expected_roi": 8.0,
        "liquidity_adjusted_roi": 4.5,
        "time_adjusted_roi": 1.0,
        "purchase_confidence": 0.8,
        "recommended_purchase_price": 12.0,
        "recommended_list_price": 1000,
        "niche": "general",
        "extension": ".com" if domain.endswith(".com") else ".net",
        "factors": factors,
        "estimated_sale_price": 1000.0,
        "sale_probability": 0.55,
        "expected_holding_months": 5.0,
        "expected_value": 550.0,
        "liquidity_grade": "A",
    }
    data.update(overrides)
    return ValuationResult(**data)


async def evaluate(tmp_path, candidate, valuation, portfolio=None, **risk_overrides):
    settings = make_settings(tmp_path, **risk_overrides)
    repository = MemoryDomainRepository()
    notifier = TelegramNotifier(settings, repository)
    policy = AcquisitionPolicy(settings, notifier, repository)
    decision = await policy.evaluate(candidate, valuation, portfolio or [])
    return decision, repository, settings


@pytest.mark.asyncio
async def test_ncshyundai_is_rejected_for_trademark_risk(tmp_path):
    candidate = make_candidate("ncshyundai.com")
    valuation = make_valuation("ncshyundai.com")

    decision, repository, _settings = await evaluate(tmp_path, candidate, valuation)

    assert decision.action == "reject"
    assert decision.trademark_risk is True
    assert decision.reason == "trademark_risk"
    assert repository.risk_events[0][1] == "reject:trademark_risk"


@pytest.mark.asyncio
async def test_safe_mode_sends_trainedrunner_to_pending_approval(tmp_path):
    candidate = make_candidate("trainedrunner.com")
    valuation = make_valuation("trainedrunner.com")

    decision, repository, settings = await evaluate(tmp_path, candidate, valuation)

    assert decision.action == "watchlist"
    assert decision.should_buy is False
    assert decision.manual_approval_required is True
    approvals = json.loads(settings.pending_approvals_file.read_text(encoding="utf-8"))
    assert approvals["trainedrunner.com"]["approved"] is False
    assert approvals["trainedrunner.com"]["reason"] == "manual_approval_required"
    assert any("Aprovacao pendente criada" in alert[1] for alert in repository.alerts)
    assert await repository.list_managed_domains() == []


@pytest.mark.asyncio
async def test_low_liquidity_net_domain_does_not_auto_buy(tmp_path):
    candidate = make_candidate("thinmarket.net", price=12.0)
    valuation = make_valuation(
        "thinmarket.net",
        expected_value=80.0,
        sale_probability=0.08,
        expected_resale_probability=0.08,
        expected_holding_months=24.0,
        liquidity_grade="D",
    )

    decision, _repository, _settings = await evaluate(tmp_path, candidate, valuation)

    assert decision.action == "reject"
    assert decision.should_buy is False
    assert decision.reason in {
        "expected_value_below_minimum",
        "liquidity_grade_not_a_or_b",
        "non_com_domain_not_allowed",
    }


@pytest.mark.asyncio
async def test_daily_and_weekly_budget_limits_block_purchases(tmp_path):
    portfolio = [
        ManagedDomain(
            name="owned.com",
            source="test",
            status=DomainStatus.REGISTERED,
            score=80,
            acquisition_cost=15.0,
            registered_at=datetime.now(UTC),
        )
    ]

    daily_decision, _daily_repo, _daily_settings = await evaluate(
        tmp_path,
        make_candidate("budgetdaily.com", price=12.0),
        make_valuation("budgetdaily.com"),
        portfolio,
        max_daily_spend_usd=20.0,
        max_weekly_spend_usd=500.0,
    )
    weekly_decision, _weekly_repo, _weekly_settings = await evaluate(
        tmp_path,
        make_candidate("budgetweekly.com", price=12.0),
        make_valuation("budgetweekly.com"),
        portfolio,
        max_daily_spend_usd=500.0,
        max_weekly_spend_usd=20.0,
    )

    assert daily_decision.action == "reject"
    assert daily_decision.reason == "max_daily_spend_reached"
    assert weekly_decision.action == "reject"
    assert weekly_decision.reason == "max_weekly_spend_reached"


class FakeApprovedScorer:
    async def value(self, candidate):
        candidate.age_years = 10
        candidate.backlinks = 500
        candidate.google_indexed = True
        candidate.keyword_value = 8
        candidate.extension_points = 10
        candidate.score = 88
        return make_valuation(
            candidate.name,
            score=88,
            expected_value=500.0,
            sale_probability=0.55,
            expected_resale_probability=0.55,
            liquidity_grade="A",
            recommended_purchase_price=12.0,
        )


class FakeScraper:
    async def scrape(self):
        return [make_candidate("dryrunner.com", score=88, price=12.0)]


class ExplodingRegistrar:
    name = "godaddy"

    def __init__(self):
        self.called = False

    async def register(self, domain, years=1):
        self.called = True
        raise AssertionError("external registrar purchase API must not be called in dry run")


class ExplodingMarketplace:
    name = "godaddy_auctions"

    def __init__(self):
        self.called = False

    async def list_domain(self, domain, price):
        self.called = True
        raise AssertionError("marketplace listing must not be called in dry run")

    async def reprice_domain(self, domain, price):
        self.called = True
        raise AssertionError("marketplace repricing must not be called in dry run")


@pytest.mark.asyncio
async def test_dry_run_purchase_records_attempt_without_external_purchase_call(tmp_path):
    approvals_file = tmp_path / "pending_approvals.json"
    attempts_file = tmp_path / "purchase_attempts.json"
    approvals_file.write_text(
        json.dumps({"dryrunner.com": {"approved": True, "approved_by": "unit-test"}}),
        encoding="utf-8",
    )
    settings = Settings(
        paper_mode=False,
        safe_mode=False,
        auto_buy_enabled=False,
        dry_run_purchases=True,
        godaddy_api_key="fake-key",
        godaddy_api_secret="fake-secret",
        pending_approvals_file=approvals_file,
        purchase_attempts_file=attempts_file,
        telegram_bot_token=None,
        telegram_chat_id=None,
        risk={
            "min_score_to_buy": 88,
            "min_expected_value": 250.0,
            "max_domain_price_usd": 15.0,
            "max_daily_spend_usd": 25.0,
            "max_weekly_spend_usd": 100.0,
            "max_buys_per_day": 1,
            "max_portfolio_domains": 10,
            "cooldown_minutes_between_buys": 120,
            "max_capital_exposure": 10_000.0,
            "allow_non_com": False,
        },
    )
    repository = MemoryDomainRepository()
    notifier = TelegramNotifier(settings, repository)
    registrar = ExplodingRegistrar()
    marketplace = ExplodingMarketplace()
    manager = DomainManager(
        settings=settings,
        scrapers=[FakeScraper()],
        scorer=FakeApprovedScorer(),
        registrar=registrar,
        marketplaces=[marketplace],
        notifier=notifier,
        repository=repository,
        event_bus=EventBus(),
    )

    monitored = await manager.run_cycle()

    assert registrar.called is False
    assert marketplace.called is False
    assert monitored[0].status == DomainStatus.WATCHLIST
    assert await repository.list_managed_domains() == []
    attempts = json.loads(attempts_file.read_text(encoding="utf-8"))
    assert set(attempts[0]) == {
        "domain",
        "price",
        "registrar",
        "approved_by",
        "timestamp",
        "blocked_by_dry_run",
        "policy_snapshot",
    }
    assert attempts[0]["domain"] == "dryrunner.com"
    assert attempts[0]["price"] == 12.0
    assert attempts[0]["registrar"] == "godaddy"
    assert attempts[0]["approved_by"] == "unit-test"
    assert attempts[0]["blocked_by_dry_run"] is True
    assert attempts[0]["policy_snapshot"]["dry_run_purchases"] is True
    assert attempts[0]["policy_snapshot"]["decision"] == "buy"
