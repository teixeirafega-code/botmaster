from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from hashlib import sha256
from typing import Any

from telegram import Bot
from telegram.error import TelegramError

from app.config.settings import Settings
from app.core.context import get_correlation_id, get_operation_id
from app.core.resilience import RetryPolicy, run_resilient
from app.db.postgres import DomainRepository
from app.models import ManagedDomain
from app.observability.metrics import runtime_status
from app.services.profit_tracker import ProfitTracker

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings, repository: DomainRepository | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self._bot: Bot | None = None
        self._alert_lock = asyncio.Lock()
        self._recent_alerts: dict[str, float] = {}
        self._alert_timestamps: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    @property
    def bot(self) -> Bot:
        if not self.settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required to send Telegram notifications")
        if self._bot is None:
            self._bot = Bot(token=self.settings.telegram_bot_token.get_secret_value())
        return self._bot

    async def send_message(self, message: str, *, disable_notification: bool = False) -> bool:
        if not await self._allow_alert(message):
            logger.warning("telegram_alert_suppressed", extra={"event_name": "telegram_alert_suppressed"})
            return False
        if not self.enabled:
            logger.info("Telegram notification skipped because TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
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
        except TelegramError:
            logger.exception("Telegram notification failed")
            raise

    async def _allow_alert(self, message: str) -> bool:
        now = time.monotonic()
        digest = sha256(message.encode()).hexdigest()
        async with self._alert_lock:
            last_sent = self._recent_alerts.get(digest)
            if last_sent and now - last_sent < self.settings.runtime.alert_cooldown_seconds:
                return False
            while self._alert_timestamps and now - self._alert_timestamps[0] > 60:
                self._alert_timestamps.popleft()
            if len(self._alert_timestamps) >= self.settings.runtime.alert_rate_limit_per_minute:
                return False
            self._recent_alerts[digest] = now
            self._alert_timestamps.append(now)
            if len(self._recent_alerts) > 1_000:
                cutoff = now - self.settings.runtime.alert_cooldown_seconds
                self._recent_alerts = {key: value for key, value in self._recent_alerts.items() if value >= cutoff}
            return True

    async def send_startup_alert(self) -> bool:
        return await self.send_message(
            f"Domain Hunter Bot started | paper_mode={self.settings.paper_mode} | bot=@saldogodaddy_bot"
        )

    async def send_rebalance_alert(self, summary: str, metadata: dict[str, Any] | None = None) -> bool:
        details = self._format_metadata(metadata)
        return await self.send_message(f"Rebalance executed\n{summary}{details}")

    async def send_apy_opportunity_alert(self, domain: str, score: int, estimated_price: int) -> bool:
        return await self.send_message(
            f"APY opportunity alert\nDomain: {domain}\nScore: {score}\nTarget list price: ${estimated_price}"
        )

    async def send_error(self, title: str, error: Exception | str, *, critical: bool = False) -> bool:
        level = "CRITICAL" if critical else "ERROR"
        logger.error("%s: %s", title, error)
        return await self.send_message(f"{level}: {title}\n{self._safe_error(error)}")

    async def send_trade_alert(
        self,
        domain: str,
        action: str,
        success: bool,
        *,
        price: float | int | None = None,
        reason: str | None = None,
    ) -> bool:
        status = "SUCCESS" if success else "FAILED"
        price_line = f"\nPrice: ${price}" if price is not None else ""
        reason_line = f"\nReason: {reason}" if reason else ""
        return await self.send_message(f"Transaction {status}\nAction: {action}\nDomain: {domain}{price_line}{reason_line}")

    async def send_sale_alert(self, domain: ManagedDomain) -> bool:
        profit = domain.sale_price - domain.acquisition_cost
        return await self.send_message(
            f"Domain sold\nDomain: {domain.name}\nSale price: ${domain.sale_price:.2f}\nCost: ${domain.acquisition_cost:.2f}\nProfit: ${profit:.2f}"
        )

    async def send_daily_report(self, domains: list[ManagedDomain]) -> bool:
        snapshot = ProfitTracker().snapshot(domains)
        return await self.send_message(
            "Daily portfolio summary\n"
            f"Domains monitored: {snapshot['domains_monitored']}\n"
            f"Registered: {snapshot['registered']}\n"
            f"Sold: {snapshot['sold']}\n"
            f"Total invested: ${snapshot['total_invested']}\n"
            f"Total profit: ${snapshot['total_profit']}\n"
            f"Portfolio value: ${snapshot['total_portfolio_value']}",
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
