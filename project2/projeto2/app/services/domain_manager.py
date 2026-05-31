from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol

from app.analyzers.scorer import DomainScorer
from app.config.settings import Settings
from app.core.context import flow_context, get_correlation_id, get_operation_id
from app.core.events import DomainEvent, EventBus, EventName
from app.db.postgres import DomainRepository
from app.economics.capital_allocator import CapitalAllocator
from app.economics.pricing import DynamicPricingEngine
from app.economics.roi import ROIOptimizer
from app.models import DomainCandidate, DomainStatus, ManagedDomain
from app.observability.metrics import runtime_status
from app.registrars.godaddy import GoDaddyRegistrar
from app.scrapers.base import BaseScraper
from app.services.acquisition_policy import AcquisitionPolicy, AcquisitionPolicyDecision
from app.services.purchase_attempts import PurchaseAttemptStore
from app.services.risk_manager import RiskManager
from app.services.telegram_notifier import TelegramNotifier
from app.services.transaction_manager import TransactionManager

logger = logging.getLogger(__name__)


class DomainMarketplace(Protocol):
    name: str

    async def list_domain(self, domain: str, price: int) -> dict[str, object]: ...

    async def reprice_domain(self, domain: str, price: int) -> dict[str, object]: ...


class DomainManager:
    def __init__(
        self,
        settings: Settings,
        scrapers: list[BaseScraper],
        scorer: DomainScorer,
        registrar: GoDaddyRegistrar,
        marketplaces: list[DomainMarketplace],
        notifier: TelegramNotifier,
        repository: DomainRepository,
        event_bus: EventBus,
        transaction_manager: TransactionManager | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self.settings = settings
        self.scrapers = scrapers
        self.scorer = scorer
        self.registrar = registrar
        self.marketplaces = marketplaces
        self.notifier = notifier
        self.repository = repository
        self.event_bus = event_bus
        self.transaction_manager = transaction_manager or TransactionManager(notifier, repository)
        self.risk_manager = risk_manager or RiskManager(settings, notifier, repository)
        self.acquisition_policy = AcquisitionPolicy(settings, notifier, repository)
        self.purchase_attempts = PurchaseAttemptStore(settings.purchase_attempts_file)
        self.roi_optimizer = ROIOptimizer(settings)
        self.capital_allocator = CapitalAllocator(settings)
        self.pricing_engine = DynamicPricingEngine(settings.pricing)
        self._cycle_lock = asyncio.Lock()
        self._scoring_semaphore = asyncio.Semaphore(settings.runtime.max_concurrent_scoring)
        self._registration_semaphore = asyncio.Semaphore(settings.runtime.max_concurrent_registrations)
        self._domain_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run_cycle(self) -> list[ManagedDomain]:
        if self._cycle_lock.locked():
            runtime_status.scheduler_skipped_overlaps += 1
            logger.warning("scheduler_overlap_skipped", extra={"event_name": "scheduler_overlap_skipped"})
            return []
        async with self._cycle_lock:
            return await asyncio.wait_for(self._run_cycle_locked(), timeout=self.settings.scheduler.cycle_timeout_seconds)

    async def _run_cycle_locked(self) -> list[ManagedDomain]:
        with flow_context(execution_mode="paper" if self.settings.paper_mode else "live"):
            logger.info("scheduler_run_started", extra={"event_name": "scheduler_run_started"})
            scraped_batches = await asyncio.gather(*(scraper.scrape() for scraper in self.scrapers), return_exceptions=True)

            candidates: dict[str, DomainCandidate] = {}
            for batch in scraped_batches:
                if isinstance(batch, Exception):
                    logger.exception("scraper_failed", exc_info=batch, extra={"event_name": "critical_failure"})
                    await self.event_bus.publish(DomainEvent(EventName.CRITICAL_FAILURE, {"provider": "scraper"}))
                    continue
                if isinstance(batch, BaseException):
                    continue
                for candidate in batch:
                    await self.repository.save_scanned(
                        candidate.name,
                        candidate.source,
                        candidate.discovered_at,
                        get_correlation_id(),
                        get_operation_id(),
                    )
                    await self.event_bus.publish(
                        DomainEvent(EventName.DOMAIN_SCANNED, {"domain": candidate.name, "source": candidate.source})
                    )
                    if not await self.repository.registration_exists(candidate.name):
                        candidates[candidate.name] = candidate

            monitored: list[ManagedDomain] = []
            portfolio = await self.load_state()
            for candidate in candidates.values():
                async with self._scoring_semaphore:
                    valuation = await self.scorer.value(candidate)
                    scored = candidate
                acquisition_decision = await self.acquisition_policy.evaluate(scored, valuation, portfolio)
                accepted = acquisition_decision.should_buy
                await self.repository.save_valuation(
                    {
                        "domain": valuation.domain,
                        "score": valuation.score,
                        "fair_market_value": valuation.fair_market_value,
                        "expected_resale_probability": valuation.expected_resale_probability,
                        "estimated_holding_days": valuation.estimated_holding_days,
                        "expected_roi": valuation.expected_roi,
                        "expected_value": valuation.expected_value,
                        "time_adjusted_roi": valuation.time_adjusted_roi,
                        "purchase_confidence": valuation.purchase_confidence,
                        "sale_probability": valuation.sale_probability,
                        "expected_holding_months": valuation.expected_holding_months,
                        "liquidity_grade": valuation.liquidity_grade,
                        "trademark_risk": valuation.trademark_risk,
                        "recommended_list_price": valuation.recommended_list_price,
                        "niche": valuation.niche,
                        "extension": valuation.extension,
                        "correlation_id": get_correlation_id(),
                        "operation_id": get_operation_id(),
                    }
                )
                await self.repository.save_scored(
                    {
                        "domain": scored.name,
                        "score": scored.score,
                        "age_years": scored.age_years,
                        "backlinks": scored.backlinks,
                        "google_indexed": scored.google_indexed,
                        "keyword_value": scored.keyword_value,
                        "extension_points": scored.extension_points,
                        "accepted": accepted,
                        "correlation_id": get_correlation_id(),
                        "operation_id": get_operation_id(),
                    }
                )
                await self.event_bus.publish(DomainEvent(EventName.DOMAIN_SCORED, {"domain": scored.name, "score": scored.score}))

                managed = ManagedDomain(
                    name=scored.name,
                    source=scored.source,
                    status=DomainStatus.WATCHLIST
                    if acquisition_decision.action == "watchlist"
                    else DomainStatus.MONITORED,
                    score=scored.score,
                )
                if acquisition_decision.should_buy and await self.risk_manager.validate_candidate(scored):
                    await self.event_bus.publish(DomainEvent(EventName.DOMAIN_APPROVED, {"domain": scored.name, "score": scored.score}))
                    try:
                        managed = await self._register_and_list(
                            scored,
                            self.pricing_engine.smart_price(scored, valuation),
                            acquisition_decision,
                        )
                        portfolio.append(managed)
                    except Exception as exc:
                        logger.exception("registration_listing_failed", extra={"event_name": "critical_failure", "domain": scored.name, "score": scored.score})
                        await self.notifier.send_error("Registration/listing failed", exc, critical=True)
                        await self.transaction_manager.mark_registration_failed(scored.name, str(exc))
                        await self.transaction_manager.report_failure(scored.name, "register_and_list", str(exc))
                        managed.status = DomainStatus.FAILED
                elif acquisition_decision.action == "reject":
                    await self.event_bus.publish(
                        DomainEvent(
                            EventName.DOMAIN_REJECTED,
                            {
                                "domain": scored.name,
                                "score": scored.score,
                                "reason": acquisition_decision.reason,
                            },
                        )
                    )
                elif acquisition_decision.should_buy:
                    await self.acquisition_policy.record_runtime_override(
                        scored,
                        valuation,
                        portfolio,
                        acquisition_decision,
                        "rejected",
                        "risk_manager_rejected",
                    )
                    await self.event_bus.publish(
                        DomainEvent(
                            EventName.DOMAIN_REJECTED,
                            {"domain": scored.name, "score": scored.score, "reason": "risk_manager_rejected"},
                        )
                    )
                monitored.append(managed)

            runtime_status.mark_scan_success(len(monitored))
            logger.info("scheduler_run_completed", extra={"event_name": "scheduler_run_completed"})
            return monitored

    async def _register_and_list(
        self,
        candidate: DomainCandidate,
        list_price: int | None = None,
        acquisition_decision: AcquisitionPolicyDecision | None = None,
    ) -> ManagedDomain:
        async with self._registration_semaphore:
            async with self._domain_locks[candidate.name]:
                price = list_price or self.settings.pricing.price_for_score(candidate.score)
                if self.settings.dry_run_purchases:
                    purchase_price = acquisition_decision.price if acquisition_decision else 0.0
                    approved_by = acquisition_decision.approved_by if acquisition_decision else None
                    policy_snapshot = (
                        self.acquisition_policy.policy_snapshot(candidate, acquisition_decision)
                        if acquisition_decision
                        else {"dry_run_purchases": self.settings.dry_run_purchases, "score": candidate.score}
                    )
                    attempt = self.purchase_attempts.record(
                        domain=candidate.name,
                        price=purchase_price,
                        registrar=self._registrar_name(),
                        approved_by=approved_by or "manual_approval_file",
                        blocked_by_dry_run=True,
                        policy_snapshot=policy_snapshot,
                    )
                    logger.info(
                        "dry_run_purchase_blocked",
                        extra={
                            "event_name": "dry_run_purchase_blocked",
                            "domain": candidate.name,
                            "score": candidate.score,
                            "price": purchase_price,
                            "registrar": attempt["registrar"],
                            "approved_by": attempt["approved_by"],
                            "blocked_by_dry_run": True,
                            "policy_snapshot": policy_snapshot,
                        },
                    )
                    await self.repository.save_risk_event(
                        candidate.name,
                        "dry_run_purchase_blocked",
                        "info",
                        get_correlation_id(),
                        get_operation_id(),
                    )
                    await self.notifier.send_dry_run_block_alert(candidate.name, candidate.score, purchase_price, self._registrar_name())
                    return ManagedDomain(
                        name=candidate.name,
                        source=candidate.source,
                        status=DomainStatus.WATCHLIST,
                        score=candidate.score,
                        asking_price=price,
                        acquisition_cost=0.0,
                        registrar=self._registrar_name(),
                    )

                reserved = await self.transaction_manager.reserve_registration(candidate.name, candidate.score)
                if not reserved:
                    runtime_status.duplicate_registrations_prevented += 1
                    logger.info(
                        "duplicate_registration_prevented",
                        extra={"event_name": "duplicate_registration_prevented", "domain": candidate.name, "score": candidate.score},
                    )
                    return ManagedDomain(
                        name=candidate.name,
                        source=candidate.source,
                        status=DomainStatus.REGISTERED,
                        score=candidate.score,
                    )

                registration = await self.registrar.register(candidate.name)
                listed_marketplaces: list[str] = []
                listing_results = await asyncio.gather(
                    *(marketplace.list_domain(candidate.name, price) for marketplace in self.marketplaces),
                    return_exceptions=True,
                )
                for marketplace, listing_result in zip(self.marketplaces, listing_results, strict=True):
                    if isinstance(listing_result, Exception):
                        logger.exception(
                            "marketplace_listing_failed",
                            exc_info=listing_result,
                            extra={"event_name": "marketplace_listing_failed", "domain": candidate.name, "marketplace": marketplace.name},
                        )
                        continue
                    listed_marketplaces.append(marketplace.name)
                    await self.repository.save_listing(
                        candidate.name,
                        marketplace.name,
                        price,
                        get_correlation_id(),
                        get_operation_id(),
                    )
                    await self.event_bus.publish(
                        DomainEvent(
                            EventName.LISTING_CREATED,
                            {"domain": candidate.name, "marketplace": marketplace.name, "score": candidate.score},
                        )
                    )

                cost = float(registration.get("cost", 12.0))
                managed = ManagedDomain(
                    name=candidate.name,
                    source=candidate.source,
                    status=DomainStatus.LISTED if listed_marketplaces else DomainStatus.REGISTERED,
                    score=candidate.score,
                    asking_price=price,
                    acquisition_cost=cost,
                    registrar="godaddy",
                    marketplaces=listed_marketplaces,
                    registered_at=datetime.now(UTC),
                    listed_at=datetime.now(UTC),
                )
                await self.transaction_manager.persist_registration(managed)
                runtime_status.domains_registered += 1
                await self.event_bus.publish(
                    DomainEvent(EventName.DOMAIN_REGISTERED, {"domain": candidate.name, "score": candidate.score})
                )
                await self.notifier.send_apy_opportunity_alert(candidate.name, candidate.score, price)
                await self.transaction_manager.report_success(candidate.name, "register_and_list", price=price)
                return managed

    def _registrar_name(self) -> str:
        return str(getattr(self.registrar, "name", "godaddy"))

    async def reprice_stale_listings(self) -> list[ManagedDomain]:
        repriced: list[ManagedDomain] = []
        portfolio = await self.load_state()
        for domain in portfolio:
            if domain.status != DomainStatus.LISTED:
                continue
            candidate = DomainCandidate(
                name=domain.name,
                source=domain.source,
                score=domain.score,
                age_years=max(0, ((datetime.now(UTC) - domain.registered_at).days // 365) if domain.registered_at else 0),
            )
            valuation = await self.scorer.value(candidate)
            new_price = self.pricing_engine.repricing_recommendation(domain, valuation)
            if not new_price or new_price == domain.asking_price:
                continue
            await asyncio.gather(
                *(marketplace.reprice_domain(domain.name, new_price) for marketplace in self.marketplaces if marketplace.name in domain.marketplaces),
                return_exceptions=True,
            )
            domain.asking_price = new_price
            domain.updated_at = datetime.now(UTC)
            repriced.append(domain)
        return repriced

    async def load_state(self) -> list[ManagedDomain]:
        return await self.repository.list_managed_domains()
