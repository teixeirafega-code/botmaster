from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.config.settings import Settings
from app.core.context import get_correlation_id, get_operation_id
from app.db.postgres import DomainRepository
from app.economics.models import ValuationResult
from app.economics.trademark import detect_trademark_risk
from app.models import DomainCandidate, DomainStatus, ManagedDomain
from app.services.manual_approval import ManualApprovalStore
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

DecisionAction = Literal["reject", "watchlist", "buy"]


@dataclass(frozen=True)
class AcquisitionPolicyDecision:
    action: DecisionAction
    reason: str
    price: float
    trademark_risk: bool
    liquidity_grade: str
    sale_probability: float
    expected_value: float
    manual_approval_required: bool = False
    approved_by: str | None = None

    @property
    def should_buy(self) -> bool:
        return self.action == "buy"


class AcquisitionPolicy:
    def __init__(
        self,
        settings: Settings,
        notifier: TelegramNotifier,
        repository: DomainRepository,
        approvals: ManualApprovalStore | None = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.repository = repository
        self.approvals = approvals or ManualApprovalStore(settings.pending_approvals_file)
        self._counters: Counter[str] = Counter()

    async def evaluate(
        self,
        candidate: DomainCandidate,
        valuation: ValuationResult,
        portfolio: list[ManagedDomain],
    ) -> AcquisitionPolicyDecision:
        price = self._purchase_price(candidate, valuation)
        trademark = detect_trademark_risk(candidate.name, self.settings.risk.famous_brands)
        trademark_risk = trademark.risky or valuation.trademark_risk
        await self._alert_candidate_signals(candidate, valuation, price, trademark_risk)
        reasons = self._blocking_reasons(candidate, valuation, portfolio, price, trademark_risk)
        if reasons:
            await self._alert_blocking_reasons(candidate, valuation, price, reasons)
            decision = AcquisitionPolicyDecision(
                "reject",
                reasons[0],
                price,
                trademark_risk,
                valuation.liquidity_grade,
                valuation.sale_probability,
                valuation.expected_value,
            )
            await self._record_decision(candidate, decision)
            return decision

        if not self.approvals.is_approved(candidate.name):
            reason = "manual_approval_required" if self.settings.safe_mode else "pending_manual_approval"
            self.approvals.upsert_pending(candidate, valuation, reason, price)
            await self.notifier.send_manual_approval_alert(candidate.name, candidate.score, price, valuation)
            if self.settings.safe_mode:
                await self.notifier.send_safe_mode_block_alert(candidate.name, candidate.score)
            decision = AcquisitionPolicyDecision(
                "watchlist",
                reason,
                price,
                trademark_risk,
                valuation.liquidity_grade,
                valuation.sale_probability,
                valuation.expected_value,
                manual_approval_required=True,
            )
            await self._record_decision(candidate, decision)
            return decision

        decision = AcquisitionPolicyDecision(
            "buy",
            "approved_for_purchase",
            price,
            trademark_risk,
            valuation.liquidity_grade,
            valuation.sale_probability,
            valuation.expected_value,
            approved_by=self.approvals.approved_by(candidate.name),
        )
        await self._record_decision(candidate, decision)
        return decision

    def policy_snapshot(self, candidate: DomainCandidate, decision: AcquisitionPolicyDecision) -> dict[str, object]:
        return {
            "safe_mode": self.settings.safe_mode,
            "auto_buy_enabled": self.settings.auto_buy_enabled,
            "dry_run_purchases": self.settings.dry_run_purchases,
            "score": candidate.score,
            "min_score_to_buy": self.settings.risk.min_score_to_buy,
            "expected_value": decision.expected_value,
            "min_expected_value": self.settings.risk.min_expected_value,
            "sale_probability": decision.sale_probability,
            "liquidity_grade": decision.liquidity_grade,
            "allowed_liquidity_grades": ["A", "B"],
            "trademark_risk": decision.trademark_risk,
            "max_domain_price_usd": self.settings.risk.max_domain_price_usd,
            "max_daily_spend_usd": self.settings.risk.max_daily_spend_usd,
            "max_weekly_spend_usd": self.settings.risk.max_weekly_spend_usd,
            "max_buys_per_day": self.settings.risk.max_buys_per_day,
            "max_portfolio_domains": self.settings.risk.max_portfolio_domains,
            "cooldown_minutes_between_buys": self.settings.risk.cooldown_minutes_between_buys,
            "decision": decision.action,
            "decision_reason": decision.reason,
        }

    def _blocking_reasons(
        self,
        candidate: DomainCandidate,
        valuation: ValuationResult,
        portfolio: list[ManagedDomain],
        price: float,
        trademark_risk: bool,
    ) -> list[str]:
        extension = "." + candidate.name.rsplit(".", 1)[-1].lower()
        reasons: list[str] = []
        if trademark_risk:
            reasons.append("trademark_risk")
        if candidate.score < self.settings.risk.min_score_to_buy:
            reasons.append("score_below_min_score_to_buy")
        if valuation.expected_value < self.settings.risk.min_expected_value:
            reasons.append("expected_value_below_minimum")
        if valuation.liquidity_grade not in {"A", "B"}:
            reasons.append("liquidity_grade_not_a_or_b")
        if extension != ".com" and not self.settings.risk.allow_non_com:
            reasons.append("non_com_domain_not_allowed")
        if price > self.settings.risk.max_domain_price_usd:
            reasons.append("price_above_max_domain_price")
        active_statuses = {DomainStatus.REGISTERED, DomainStatus.LISTED}
        active_portfolio_size = len([domain for domain in portfolio if domain.status in active_statuses])
        if active_portfolio_size >= self.settings.risk.max_portfolio_domains:
            reasons.append("max_portfolio_domains_reached")
        if self._buys_today(portfolio) >= self.settings.risk.max_buys_per_day:
            reasons.append("max_buys_per_day_reached")
        if self._spend_since(portfolio, timedelta(days=1)) + price > self.settings.risk.max_daily_spend_usd:
            reasons.append("max_daily_spend_reached")
        if self._spend_since(portfolio, timedelta(days=7)) + price > self.settings.risk.max_weekly_spend_usd:
            reasons.append("max_weekly_spend_reached")
        last_buy = self._last_buy_at(portfolio)
        if last_buy and datetime.now(UTC) - last_buy < timedelta(minutes=self.settings.risk.cooldown_minutes_between_buys):
            reasons.append("cooldown_between_buys_active")
        return reasons

    async def _alert_candidate_signals(
        self,
        candidate: DomainCandidate,
        valuation: ValuationResult,
        price: float,
        trademark_risk: bool,
    ) -> None:
        if candidate.score >= 90:
            await self.notifier.send_candidate_signal_alert("score_90_candidate", candidate.name, candidate.score, valuation)
        if valuation.liquidity_grade == "A":
            await self.notifier.send_candidate_signal_alert("liquidity_grade_a_candidate", candidate.name, candidate.score, valuation)
        if trademark_risk:
            await self.notifier.send_policy_block_alert(
                "trademark_risk_detected",
                candidate.name,
                candidate.score,
                "trademark_risk",
                price=price,
                valuation=valuation,
            )

    async def _alert_blocking_reasons(
        self,
        candidate: DomainCandidate,
        valuation: ValuationResult,
        price: float,
        reasons: list[str],
    ) -> None:
        for reason in reasons:
            event_type = self._alert_event_type_for_reason(reason)
            if not event_type:
                continue
            await self.notifier.send_policy_block_alert(
                event_type,
                candidate.name,
                candidate.score,
                reason,
                price=price,
                valuation=valuation,
            )

    def _alert_event_type_for_reason(self, reason: str) -> str | None:
        if reason == "max_weekly_spend_reached":
            return "weekly_spend_limit_reached"
        if reason in {"max_daily_spend_reached", "max_buys_per_day_reached", "max_portfolio_domains_reached", "price_above_max_domain_price"}:
            return "budget_limit_reached"
        if reason == "cooldown_between_buys_active":
            return "cooldown_limit_reached"
        return None

    def _purchase_price(self, candidate: DomainCandidate, valuation: ValuationResult) -> float:
        for key in ("price", "current_bid", "auction_price", "purchase_price", "cost"):
            raw = candidate.source_metadata.get(key)
            try:
                if raw not in (None, ""):
                    return float(str(raw).replace(",", "").replace("$", ""))
            except ValueError:
                continue
        return max(0.0, float(valuation.recommended_purchase_price or 12.0))

    def _spend_since(self, portfolio: list[ManagedDomain], window: timedelta) -> float:
        cutoff = datetime.now(UTC) - window
        return sum(domain.acquisition_cost for domain in portfolio if domain.registered_at and domain.registered_at >= cutoff)

    def _buys_today(self, portfolio: list[ManagedDomain]) -> int:
        today = datetime.now(UTC).date()
        return sum(1 for domain in portfolio if domain.registered_at and domain.registered_at.date() == today)

    def _last_buy_at(self, portfolio: list[ManagedDomain]) -> datetime | None:
        dates = [domain.registered_at for domain in portfolio if domain.registered_at]
        return max(dates) if dates else None

    async def _record_decision(self, candidate: DomainCandidate, decision: AcquisitionPolicyDecision) -> None:
        self._counters["decisions_total"] += 1
        self._counters[f"{decision.action}_total"] += 1
        self._counters[f"reason:{decision.reason}"] += 1
        logger.info(
            "acquisition_decision",
            extra={
                "event_name": "acquisition_decision",
                "domain": candidate.name,
                "score": candidate.score,
                "trademark_risk": decision.trademark_risk,
                "liquidity_grade": decision.liquidity_grade,
                "sale_probability": decision.sale_probability,
                "expected_value": decision.expected_value,
                "price": decision.price,
                "decision": decision.action,
                "decision_reason": decision.reason,
            },
        )
        await self.repository.save_risk_event(
            candidate.name,
            f"{decision.action}:{decision.reason}",
            "info" if decision.action != "reject" else "warning",
            get_correlation_id(),
            get_operation_id(),
        )

    def counters_snapshot(self) -> dict[str, int]:
        return dict(self._counters)
