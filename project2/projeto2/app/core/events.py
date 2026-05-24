from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.context import get_correlation_id, get_operation_id

logger = logging.getLogger(__name__)
EventHandler = Callable[["DomainEvent"], Awaitable[None]]
event_depth_var: contextvars.ContextVar[int] = contextvars.ContextVar("event_depth", default=0)


class EventName(StrEnum):
    DOMAIN_SCANNED = "DOMAIN_SCANNED"
    DOMAIN_SCORED = "DOMAIN_SCORED"
    DOMAIN_APPROVED = "DOMAIN_APPROVED"
    DOMAIN_REJECTED = "DOMAIN_REJECTED"
    DOMAIN_REGISTERED = "DOMAIN_REGISTERED"
    LISTING_CREATED = "LISTING_CREATED"
    ALERT_TRIGGERED = "ALERT_TRIGGERED"
    CRITICAL_FAILURE = "CRITICAL_FAILURE"


@dataclass(frozen=True)
class DomainEvent:
    name: EventName
    payload: dict[str, Any]
    correlation_id: str = field(default_factory=get_correlation_id)
    operation_id: str = field(default_factory=get_operation_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class DeadLetterEvent:
    event: DomainEvent
    reason: str
    failed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventBus:
    def __init__(self, *, handler_timeout_seconds: float = 10.0, dead_letter_max: int = 500, max_depth: int = 3) -> None:
        self._handlers: dict[EventName, list[EventHandler]] = defaultdict(list)
        self._handler_timeout_seconds = handler_timeout_seconds
        self._dead_letter_max = dead_letter_max
        self._max_depth = max_depth
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self.dead_letters: list[DeadLetterEvent] = []

    def subscribe(self, event_name: EventName, handler: EventHandler) -> None:
        self._handlers[event_name].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        from app.observability.metrics import runtime_status

        if not isinstance(event.name, EventName):
            self._dead_letter(event, "invalid_event_name")
            return
        if event.event_id in self._seen:
            logger.warning("duplicate_event_dropped", extra={"event_name": event.name.value})
            return
        depth = event_depth_var.get()
        if depth >= self._max_depth:
            self._dead_letter(event, "max_event_recursion_depth")
            return
        self._seen.add(event.event_id)
        self._seen_order.append(event.event_id)
        if len(self._seen_order) > 10_000:
            expired = self._seen_order.pop(0)
            self._seen.discard(expired)

        handlers = self._handlers.get(event.name, [])
        runtime_status.event_published += 1
        logger.info("event_published", extra={"event_name": event.name.value, **event.payload})
        if not handlers:
            return
        token = event_depth_var.set(depth + 1)
        try:
            results = await asyncio.gather(
                *(asyncio.wait_for(handler(event), timeout=self._handler_timeout_seconds) for handler in handlers),
                return_exceptions=True,
            )
        finally:
            event_depth_var.reset(token)
        for result in results:
            if isinstance(result, Exception):
                self._dead_letter(event, result.__class__.__name__)
                logger.exception("event_handler_failed", exc_info=result, extra={"event_name": event.name.value})

    def _dead_letter(self, event: DomainEvent, reason: str) -> None:
        from app.observability.metrics import runtime_status

        self.dead_letters.append(DeadLetterEvent(event=event, reason=reason))
        if len(self.dead_letters) > self._dead_letter_max:
            self.dead_letters.pop(0)
        runtime_status.event_dead_letters += 1
        logger.error("event_dead_lettered", extra={"event_name": event.name.value, "reason": reason})
