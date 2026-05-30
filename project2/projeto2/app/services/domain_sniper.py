from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models import DomainCandidate, ManagedDomain
from app.registrars.dropcatch import DropCatchClient
from app.services.domain_manager import DomainManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SniperAttempt:
    domain: str
    target_time: datetime
    attempts: int
    success: bool
    result: ManagedDomain | None = None
    error: str = ""


class DomainSniper:
    def __init__(self, manager: DomainManager, dropcatch: DropCatchClient | None = None) -> None:
        self.manager = manager
        self.dropcatch = dropcatch or DropCatchClient(manager.settings)
        self.refresh_seconds = 30
        self.window = timedelta(hours=1)
        self.retry_attempts = 6
        self.retry_delay_seconds = 0.25

    async def scan_once(self) -> list[DomainCandidate]:
        now = datetime.now(UTC)
        limit = now + self.window
        batches = await asyncio.gather(*(scraper.scrape() for scraper in self.manager.scrapers), return_exceptions=True)
        candidates: dict[str, DomainCandidate] = {}
        for batch in batches:
            if isinstance(batch, Exception):
                logger.exception("sniper_scraper_failed", exc_info=batch, extra={"event_name": "sniper_scraper_failed"})
                continue
            if isinstance(batch, BaseException):
                continue
            for candidate in batch:
                target_time = self._target_time(candidate)
                if target_time and now <= target_time <= limit:
                    candidates[candidate.name] = candidate
        return sorted(candidates.values(), key=lambda item: self._target_time(item) or limit)

    async def monitor(self, cycles: int | None = None) -> list[SniperAttempt]:
        results: list[SniperAttempt] = []
        seen: set[str] = set()
        tasks: list[asyncio.Task[None]] = []
        remaining_cycles = cycles
        while remaining_cycles is None or remaining_cycles > 0:
            candidates = [candidate for candidate in await self.scan_once() if candidate.name not in seen]
            for candidate in candidates:
                seen.add(candidate.name)
                tasks.append(asyncio.create_task(self._prepare_and_snipe(candidate, results)))
            if remaining_cycles is not None:
                remaining_cycles -= 1
                if remaining_cycles <= 0:
                    break
            await asyncio.sleep(self.refresh_seconds)
        if tasks:
            await asyncio.gather(*tasks)
        return results

    async def _prepare_and_snipe(self, candidate: DomainCandidate, results: list[SniperAttempt]) -> None:
        target_time = self._target_time(candidate)
        if not target_time:
            return
        valuation = await self.manager.scorer.value(candidate)
        acquisition_decision = await self.manager.acquisition_policy.evaluate(
            candidate,
            valuation,
            await self.manager.load_state(),
        )
        if not acquisition_decision.should_buy:
            return
        price = self.manager.pricing_engine.smart_price(candidate, valuation)
        await self.dropcatch.place_backorder(candidate.name, max_bid=valuation.recommended_purchase_price)
        results.append(await self.attempt_registration_at_expiry(candidate, target_time, price))

    async def attempt_registration_at_expiry(self, candidate: DomainCandidate, target_time: datetime, list_price: int) -> SniperAttempt:
        target_time = target_time.astimezone(UTC)
        await self._sleep_until(target_time)
        valuation = await self.manager.scorer.value(candidate)
        acquisition_decision = await self.manager.acquisition_policy.evaluate(candidate, valuation, await self.manager.load_state())
        if not acquisition_decision.should_buy:
            return SniperAttempt(candidate.name, target_time, 0, False, error=acquisition_decision.reason)
        if not await self.manager.risk_manager.validate_candidate(candidate):
            return SniperAttempt(candidate.name, target_time, 0, False, error="risk_manager_rejected")
        last_error = ""
        for attempt in range(1, self.retry_attempts + 1):
            try:
                result = await self.manager._register_and_list(candidate, list_price, acquisition_decision)
                return SniperAttempt(candidate.name, target_time, attempt, True, result=result)
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "sniper_registration_retry",
                    extra={"event_name": "sniper_registration_retry", "domain": candidate.name, "attempt": attempt},
                )
                await asyncio.sleep(self.retry_delay_seconds)
        return SniperAttempt(candidate.name, target_time, self.retry_attempts, False, error=last_error)

    async def _sleep_until(self, target_time: datetime) -> None:
        now = datetime.now(UTC)
        delta = (target_time - now).total_seconds()
        if delta <= 0:
            return
        if delta > 0.05:
            await asyncio.sleep(delta - 0.02)
        target_perf = time.perf_counter() + max(0.0, (target_time - datetime.now(UTC)).total_seconds())
        while time.perf_counter() < target_perf:
            pass

    def _target_time(self, candidate: DomainCandidate) -> datetime | None:
        target = candidate.expires_at or candidate.auction_end_at
        if target and target.tzinfo is None:
            return target.replace(tzinfo=UTC)
        return target.astimezone(UTC) if target else None
