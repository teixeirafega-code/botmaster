from __future__ import annotations

import logging

from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class RebalanceEngine:
    def __init__(self, notifier: TelegramNotifier) -> None:
        self.notifier = notifier

    async def execute(self, summary: str, metadata: dict[str, object] | None = None) -> None:
        logger.info("Rebalance execution completed: %s", summary)
        await self.notifier.send_rebalance_alert(summary, metadata)

