from __future__ import annotations

from typing import Protocol

from app.models import DomainCandidate


class BaseScraper(Protocol):
    async def scrape(self) -> list[DomainCandidate]: ...


BaseScaper = BaseScraper
