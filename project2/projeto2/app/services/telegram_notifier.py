from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from telegram import Bot
from telegram.error import TelegramError

from app.config.settings import Settings
from app.core.context import get_correlation_id, get_operation_id
from app.core.resilience import RetryPolicy, run_resilient
from app.db.postgres import DomainRepository
from app.economics.models import ValuationResult
from app.models import ManagedDomain
from app.observability.metrics import runtime_status
from app.services.profit_tracker import ProfitTracker

logger = logging.getLogger(__name__)

EVENT_COOLDOWN_SECONDS = 900


class TelegramNotifier:
    def __init__(self, settings: Settings, repository: DomainRepository | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self._bot: Bot | None = None
        self._alert_lock = asyncio.Lock()
        self._recent_alerts: dict[str, float] = {}
        self._event_alerts: dict[str, float] = {}
        self._alert_timestamps: deque[float] = deque()
        self._startup_health_sent = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_enabled and self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    @property
    def bot(self) -> Bot:
        if not self.settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required to send Telegram notifications")
        if self._bot is None:
            self._bot = Bot(token=self.settings.telegram_bot_token.get_secret_value())
        return self._bot

    async def send_alert(self, event_type: str, message: str, *, disable_notification: bool = False) -> bool:
        if not await self._allow_alert(message, event_type=event_type):
            logger.warning(
                "telegram_alert_suppressed",
                extra={"event_name": "telegram_alert_suppressed", "telegram_event_type": event_type},
            )
            return False
        if not self.enabled:
            logger.info("Telegram notification skipped because TELEGRAM_ENABLED is false or credentials are missing")
            if self.repository:
                await self.repository.save_alert("telegram", message, False, get_correlation_id(), get_operation_id())
            return False
        assert self.settings.telegram_chat_id is not None
        chat_id = self.settings.telegram_chat_id
        try:
            await run_resilient(
                "telegram",
                lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    disable_web_page_preview=True,
                    disable_notification=disable_notification,
                ),
                policy=RetryPolicy(attempts=3, base_delay_seconds=1.0, timeout_seconds=15.0),
                retry_exceptions=(TelegramError, TimeoutError, OSError),
            )
            runtime_status.alerts_sent += 1
            if self.repository:
                await self.repository.save_alert("telegram", message, True, get_correlation_id(), get_operation_id())
            return True
        except Exception as exc:
            logger.error("Telegram notification failed: %s", self._safe_error(exc))
            if self.repository:
                await self.repository.save_alert("telegram", message, False, get_correlation_id(), get_operation_id())
            return False

    async def send_message(self, message: str, *, disable_notification: bool = False) -> bool:
        return await self.send_alert("generic", message, disable_notification=disable_notification)

    async def _allow_alert(self, message: str, *, event_type: str | None = None) -> bool:
        now = time.monotonic()
        digest = sha256(message.encode()).hexdigest()
        async with self._alert_lock:
            if event_type:
                last_event = self._event_alerts.get(event_type)
                if last_event and now - last_event < EVENT_COOLDOWN_SECONDS:
                    return False
            last_sent = self._recent_alerts.get(digest)
            if last_sent and now - last_sent < self.settings.runtime.alert_cooldown_seconds:
                return False
            while self._alert_timestamps and now - self._alert_timestamps[0] > 60:
                self._alert_timestamps.popleft()
            if len(self._alert_timestamps) >= self.settings.runtime.alert_rate_limit_per_minute:
                return False
            self._recent_alerts[digest] = now
            self._alert_timestamps.append(now)
            if event_type:
                self._event_alerts[event_type] = now
            if len(self._recent_alerts) > 1_000:
                cutoff = now - self.settings.runtime.alert_cooldown_seconds
                self._recent_alerts = {key: value for key, value in self._recent_alerts.items() if value >= cutoff}
            if len(self._event_alerts) > 1_000:
                event_cutoff = now - EVENT_COOLDOWN_SECONDS
                self._event_alerts = {key: value for key, value in self._event_alerts.items() if value >= event_cutoff}
            return True

    async def send_startup_alert(self) -> bool:
        return await self.send_startup_health_check()

    async def send_startup_health_check(self) -> bool:
        if self._startup_health_sent or not self.enabled:
            return False
        self._startup_health_sent = True
        safe_mode = "ON" if self.settings.safe_mode else "OFF"
        dry_run = "ON" if self.settings.dry_run_purchases else "OFF"
        message = (
            "✅ Domain Hunter started successfully.\n"
            f"SAFE_MODE={safe_mode}\n"
            f"DRY_RUN_PURCHASES={dry_run}\n"
            f"Timestamp: {datetime.now(UTC).isoformat()}"
        )
        return await self.send_alert("startup_health_check", message)

    async def send_bot_restart_alert(self, message: str = "Domain Hunter Bot restarted") -> bool:
        alert = (
            f"Bot restart\n{message}\n"
            f"Paper mode: {self.settings.paper_mode}\n"
            f"SAFE_MODE: {self.settings.safe_mode}\n"
            f"DRY_RUN_PURCHASES: {self.settings.dry_run_purchases}"
        )
        return await self.send_alert(
            "bot_restart",
            alert,
        )

    async def send_rebalance_alert(self, summary: str, metadata: dict[str, Any] | None = None) -> bool:
        details = self._format_metadata(metadata)
        return await self.send_alert("critical_exception", f"Rebalance alert\n{summary}{details}")

    async def send_apy_opportunity_alert(self, domain: str, score: int, estimated_price: int) -> bool:
        return await self.send_alert(
            "real_purchase",
            f"Real purchase/listing event\nDomain: {domain}\nScore: {score}\nTarget list price: ${estimated_price}",
        )

    async def send_manual_approval_alert(
        self,
        domain: str,
        score: int,
        purchase_price: float,
        valuation: ValuationResult,
    ) -> bool:
        return await self.send_alert(
            "pending_approval_created",
            "Pending approval created\n"
            f"Domain: {domain}\n"
            f"Score: {score}\n"
            f"Purchase price: ${purchase_price:.2f}\n"
            f"Expected value: ${valuation.expected_value:.2f}\n"
            f"Sale probability: {valuation.sale_probability:.1%}\n"
            f"Liquidity: {valuation.liquidity_grade}\n"
            f"Trademark risk: {valuation.trademark_risk}\n"
            f"Approval file: {self.settings.pending_approvals_file}\n"
            "Notifications only. Telegram commands cannot approve or buy domains.",
        )

    async def send_candidate_signal_alert(self, event_type: str, domain: str, score: int, valuation: ValuationResult) -> bool:
        label = "High-score candidate" if event_type == "score_90_candidate" else "Liquidity grade A candidate"
        return await self.send_alert(
            event_type,
            f"{label}\n"
            f"Domain: {domain}\n"
            f"Score: {score}\n"
            f"Liquidity: {valuation.liquidity_grade}\n"
            f"Expected value: ${valuation.expected_value:.2f}\n"
            f"Sale probability: {valuation.sale_probability:.1%}\n"
            "Notifications only. No Telegram approval commands are accepted.",
        )

    async def send_policy_block_alert(
        self,
        event_type: str,
        domain: str,
        score: int,
        reason: str,
        *,
        price: float | None = None,
        valuation: ValuationResult | None = None,
    ) -> bool:
        price_line = f"\nPrice: ${price:.2f}" if price is not None else ""
        valuation_line = (
            f"\nLiquidity: {valuation.liquidity_grade}\nExpected value: ${valuation.expected_value:.2f}"
            if valuation is not None
            else ""
        )
        return await self.send_alert(
            event_type,
            f"Policy block\nDomain: {domain}\nScore: {score}\nReason: {reason}{price_line}{valuation_line}",
        )

    async def send_safe_mode_block_alert(self, domain: str, score: int) -> bool:
        return await self.send_alert(
            "safe_mode_block",
            f"SAFE_MODE block\nDomain: {domain}\nScore: {score}\nManual approval file required. Telegram cannot approve or buy.",
        )

    async def send_dry_run_block_alert(self, domain: str, score: int, price: float, registrar: str) -> bool:
        return await self.send_alert(
            "dry_run_block",
            f"DRY_RUN block\nDomain: {domain}\nScore: {score}\nPrice: ${price:.2f}\nRegistrar: {registrar}\nNo purchase API was called.",
        )

    async def send_error(self, title: str, error: Exception | str, *, critical: bool = False) -> bool:
        level = "CRITICAL" if critical else "ERROR"
        logger.error("%s: %s", title, self._safe_error(error))
        event_type = "critical_exception" if critical else "error"
        return await self.send_alert(event_type, f"{level}: {title}\n{self._safe_error(error)}")

    async def send_trade_alert(
        self,
        domain: str,
        action: str,
        success: bool,
        *,
        price: float | int | None = None,
        reason: str | None = None,
    ) -> bool:
        if success:
            return False
        price_line = f"\nPrice: ${price}" if price is not None else ""
        reason_line = f"\nReason: {reason}" if reason else ""
        return await self.send_alert(
            f"transaction_failure:{action}",
            f"Transaction FAILED\nAction: {action}\nDomain: {domain}{price_line}{reason_line}",
        )

    async def send_sale_alert(self, domain: ManagedDomain) -> bool:
        profit = domain.sale_price - domain.acquisition_cost
        return await self.send_alert(
            "domain_sold",
            f"Domain sold\nDomain: {domain.name}\nSale price: ${domain.sale_price:.2f}\nCost: ${domain.acquisition_cost:.2f}\nProfit: ${profit:.2f}",
        )

    async def send_daily_report(self, domains: list[ManagedDomain]) -> bool:
        snapshot = ProfitTracker().snapshot(domains)
        return await self.send_alert(
            "daily_summary",
            "Daily portfolio summary\n"
            f"Domains monitored: {snapshot['domains_monitored']}\n"
            f"Registered: {snapshot['registered']}\n"
            f"Sold: {snapshot['sold']}\n"
            f"Total invested: ${snapshot['total_invested']}\n"
            f"Total profit: ${snapshot['total_profit']}\n"
            f"Portfolio value: ${snapshot['total_portfolio_value']}",
            disable_notification=True,
        )

    async def send_domain_daily_summary(self, summary: dict[str, Any]) -> bool:
        return await self.send_alert(
            "daily_summary",
            "Daily Domain Hunter summary\n"
            f"Domains scanned: {summary.get('domains_scanned', 0)}\n"
            f"Opportunities found: {summary.get('opportunities_found', 0)}\n"
            f"Pending approvals: {summary.get('pending_approvals', 0)}\n"
            f"Trademark blocks: {summary.get('trademark_blocks', 0)}\n"
            f"Liquidity blocks: {summary.get('liquidity_blocks', 0)}\n"
            f"Budget blocks: {summary.get('budget_blocks', 0)}\n"
            f"Manual approvals: {summary.get('manual_approvals', 0)}\n"
            f"Real purchases: {summary.get('real_purchases', 0)}",
            disable_notification=True,
        )

    def _format_metadata(self, metadata: dict[str, Any] | None) -> str:
        if not metadata:
            return ""
        safe_lines = [f"{key}: {value}" for key, value in metadata.items() if "token" not in key.lower() and "secret" not in key.lower()]
        return "\n" + "\n".join(safe_lines) if safe_lines else ""

    def _safe_error(self, error: Exception | str) -> str:
        message = str(error)
        for secret in (
            self.settings.telegram_bot_token,
            self.settings.godaddy_api_key,
            self.settings.godaddy_api_secret,
            self.settings.sedo_api_key,
            self.settings.afternic_api_key,
        ):
            if secret:
                message = message.replace(secret.get_secret_value(), "[redacted]")
        return message
