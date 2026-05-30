from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from app.config.settings import Settings
from app.core.context import get_correlation_id, get_operation_id
from app.db.postgres import DomainRepository
from app.economics.trademark import detect_trademark_risk
from app.models import DomainCandidate
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]+(?<!-)\.(com|net|org|io)$", re.IGNORECASE)


class RiskManager:
    def __init__(self, settings: Settings, notifier: TelegramNotifier, repository: DomainRepository) -> None:
        self.settings = settings
        self.notifier = notifier
        self.repository = repository
        self._daily_registrations = 0
        self._capital_exposure = 0.0
        self._last_approval: dict[str, datetime] = {}
        self._current_day = datetime.now(UTC).date()
        self._lock = asyncio.Lock()

    async def validate_candidate(self, candidate: DomainCandidate) -> bool:
        async with self._lock:
            self._reset_daily_window_if_needed()
            reason = self._rejection_reason(candidate)
            if reason:
                await self.repository.save_risk_event(candidate.name, reason, "warning", get_correlation_id(), get_operation_id())
                if self.settings.risk.dry_run_audit:
                    logger.info(
                        "risk_dry_run_audit",
                        extra={"event_name": "risk_dry_run_audit", "domain": candidate.name, "score": candidate.score},
                    )
                    return True
                return False
            self._daily_registrations += 1
            self._capital_exposure += self.settings.pricing.price_for_score(candidate.score)
            self._last_approval[candidate.name] = datetime.now(UTC)
            return True

    def _rejection_reason(self, candidate: DomainCandidate) -> str | None:
        if self.settings.risk.emergency_stop:
            return "emergency_stop_enabled"
        if not DOMAIN_RE.fullmatch(candidate.name):
            return "malformed_domain"
        if detect_trademark_risk(candidate.name, self.settings.risk.famous_brands).risky:
            return "trademark_risk"
        if candidate.score < 0 or candidate.score > 100:
            return "invalid_score_range"
        component_sum = (
            min(30, candidate.age_years * 3)
            + min(25, candidate.backlinks // 20)
            + (15 if candidate.google_indexed else 0)
            + candidate.keyword_value
            + candidate.extension_points
        )
        if abs(component_sum - candidate.score) > 10:
            return "score_component_anomaly"
        if datetime.now(UTC) - candidate.discovered_at > timedelta(minutes=self.settings.risk.max_candidate_age_minutes):
            return "candidate_data_stale"
        if candidate.name in set(self.settings.risk.blacklist):
            return "domain_blacklisted"
        if candidate.score < self.settings.risk.minimum_score:
            return "score_below_risk_threshold"
        if self._daily_registrations >= self.settings.risk.max_daily_registrations:
            return "max_daily_registrations_reached"
        projected_exposure = self._capital_exposure + self.settings.pricing.price_for_score(candidate.score)
        if projected_exposure > self.settings.risk.max_capital_exposure:
            return "max_capital_exposure_reached"
        last_approval = self._last_approval.get(candidate.name)
        if last_approval and datetime.now(UTC) - last_approval < timedelta(minutes=self.settings.risk.cooldown_minutes):
            return "domain_cooldown_active"
        return None

    def _reset_daily_window_if_needed(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._current_day:
            self._current_day = today
            self._daily_registrations = 0
            self._capital_exposure = 0.0

    async def critical_error(self, title: str, error: Exception | str) -> None:
        logger.critical("%s: %s", title, error)
        await self.notifier.send_error(title, error, critical=True)
