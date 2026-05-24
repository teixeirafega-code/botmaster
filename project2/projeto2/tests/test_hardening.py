import asyncio

import pytest

from app.config.settings import Settings
from app.core.events import DomainEvent, EventBus, EventName
from app.db.postgres import MemoryDomainRepository
from app.models import DomainCandidate
from app.services.risk_manager import RiskManager
from app.services.telegram_notifier import TelegramNotifier
from app.services.transaction_manager import TransactionManager


@pytest.mark.asyncio
async def test_memory_repository_registration_reservation_is_atomic():
    repository = MemoryDomainRepository()
    notifier = TelegramNotifier(Settings(), repository)
    manager = TransactionManager(notifier, repository)

    results = await asyncio.gather(
        *(manager.reserve_registration("race-test.com", 80) for _ in range(25))
    )

    assert results.count(True) == 1
    assert results.count(False) == 24


@pytest.mark.asyncio
async def test_event_bus_drops_duplicate_event_ids():
    seen = 0
    bus = EventBus()

    async def handler(event: DomainEvent) -> None:
        nonlocal seen
        seen += 1

    bus.subscribe(EventName.DOMAIN_SCANNED, handler)
    event = DomainEvent(EventName.DOMAIN_SCANNED, {"domain": "example.com"}, event_id="same-event")

    await bus.publish(event)
    await bus.publish(event)

    assert seen == 1
    assert bus.dead_letters == []


@pytest.mark.asyncio
async def test_event_bus_dead_letters_slow_handler():
    bus = EventBus(handler_timeout_seconds=0.01)

    async def slow_handler(event: DomainEvent) -> None:
        await asyncio.sleep(1)

    bus.subscribe(EventName.DOMAIN_SCANNED, slow_handler)
    await bus.publish(DomainEvent(EventName.DOMAIN_SCANNED, {"domain": "example.com"}))

    assert len(bus.dead_letters) == 1


@pytest.mark.asyncio
async def test_telegram_alert_deduplication():
    repository = MemoryDomainRepository()
    settings = Settings(telegram_bot_token=None, telegram_chat_id=None)
    notifier = TelegramNotifier(settings, repository)

    assert await notifier.send_message("same alert") is False
    assert await notifier.send_message("same alert") is False
    assert len(repository.alerts) == 1


@pytest.mark.asyncio
async def test_risk_rejects_malformed_domain():
    repository = MemoryDomainRepository()
    notifier = TelegramNotifier(Settings(), repository)
    risk = RiskManager(Settings(), notifier, repository)

    allowed = await risk.validate_candidate(
        DomainCandidate(name="-bad.com", source="test", score=90, age_years=10, backlinks=500)
    )

    assert allowed is False
    assert repository.risk_events[0][1] == "malformed_domain"
