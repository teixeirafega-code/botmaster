import pytest

from app.config.settings import Settings
from app.db.postgres import MemoryDomainRepository
from app.models import DomainStatus, ManagedDomain
from app.services.telegram_notifier import TelegramNotifier
from app.services.transaction_manager import TransactionManager


@pytest.mark.asyncio
async def test_transaction_idempotency_prevents_duplicate_registration():
    repository = MemoryDomainRepository()
    notifier = TelegramNotifier(Settings(telegram_bot_token=None, telegram_chat_id=None), repository)
    manager = TransactionManager(notifier, repository)
    domain = ManagedDomain(name="example.com", source="test", status=DomainStatus.LISTED, score=80)

    key_one = await manager.persist_registration(domain)
    key_two = await manager.persist_registration(domain)

    assert key_one == key_two
    assert await manager.registration_already_processed("example.com") is True
    assert len(await repository.list_managed_domains()) == 1


@pytest.mark.asyncio
async def test_failed_reserved_registration_is_marked_failed():
    repository = MemoryDomainRepository()
    notifier = TelegramNotifier(Settings(telegram_bot_token=None, telegram_chat_id=None), repository)
    manager = TransactionManager(notifier, repository)

    assert await manager.reserve_registration("failed.com", 80) is True
    await manager.mark_registration_failed("failed.com", "provider timeout")

    domains = await repository.list_managed_domains()
    assert domains[0].status == DomainStatus.FAILED
    assert repository.risk_events[0][1] == "provider timeout"
