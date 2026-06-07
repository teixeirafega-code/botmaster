from __future__ import annotations

import logging
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)


class TelegramAlertClient:
    def __init__(self, config: dict[str, Any]):
        telegram = config.get("monitoring", {}).get("telegram", {})
        self.enabled = bool(telegram.get("enabled", False))
        self.bot_token = str(telegram.get("bot_token", "") or "")
        self.chat_id = str(telegram.get("chat_id", "") or "")
        self.timeout = float(telegram.get("timeout_seconds", 10))

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)

    def send(self, event: str, message: str) -> bool:
        if not self.configured:
            LOGGER.info("Telegram alert skipped for %s: Telegram is disabled or not configured", event)
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": f"ShortsMaster Bot - {event}\n{message}",
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            LOGGER.warning("Telegram alert failed for %s: %s", event, exc)
            return False
