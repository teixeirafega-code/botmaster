from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import TrendTopic


class MonitorError(RuntimeError):
    """Raised when a trend monitor cannot fetch or parse its source."""


class TrendMonitor(ABC):
    source_name: str

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def fetch(self) -> list[TrendTopic]:
        """Return normalized trend topics from the monitor source."""
