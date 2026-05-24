from __future__ import annotations

import logging
import uuid

from app.core.context import get_correlation_id, get_operation_id
from app.db.postgres import DomainRepository
from app.models import ManagedDomain
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TransactionManager:
    def __init__(self, notifier: TelegramNotifier, repository: DomainRepository | None = None) -> None:
        self.notifier = notifier
        self.repository = repository

    async def report_success(self, domain: str, action: str, price: float | int | None = None) -> None:
        logger.info("Transaction success: %s %s", action, domain)
        await self.notifier.send_trade_alert(domain, action, True, price=price)

    async def report_failure(self, domain: str, action: str, reason: str) -> None:
        logger.error("Transaction failure: %s %s: %s", action, domain, reason)
        await self.notifier.send_trade_alert(domain, action, False, reason=reason)

    def idempotency_key(self, domain: str, action: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"domain-hunter:{action}:{domain}"))

    async def registration_already_processed(self, domain: str) -> bool:
        if not self.repository:
            return False
        return await self.repository.registration_exists(domain)

    async def reserve_registration(self, domain: str, score: int, action: str = "register_and_list") -> bool:
        if not self.repository:
            return True
        return await self.repository.try_reserve_registration(
            domain,
            "godaddy",
            score,
            self.idempotency_key(domain, action),
            get_correlation_id(),
            get_operation_id(),
        )

    async def persist_registration(self, managed: ManagedDomain, action: str = "register_and_list") -> str:
        key = self.idempotency_key(managed.name, action)
        if self.repository:
            await self.repository.save_registration(managed, key, get_correlation_id(), get_operation_id())
        return key

    async def mark_registration_failed(self, domain: str, reason: str) -> None:
        if self.repository:
            await self.repository.mark_registration_failed(domain, reason, get_correlation_id(), get_operation_id())
