from __future__ import annotations

import logging
from collections.abc import Iterable

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient
from app.domain_sources import (
    candidates_from_csv_text,
    candidates_from_html_links,
    candidates_from_json_payload,
    get_bytes,
    iter_zip_texts,
)
from app.models import DomainCandidate

logger = logging.getLogger(__name__)


class CsvAuctionScraper:
    source = "auction_csv"

    def __init__(self, settings: Settings, urls: Iterable[str], source: str) -> None:
        self.settings = settings
        self.urls = tuple(urls)
        self.source = source

    async def scrape(self) -> list[DomainCandidate]:
        async def fetch() -> list[DomainCandidate]:
            candidates: list[DomainCandidate] = []
            seen: set[str] = set()
            for url in self.urls:
                try:
                    payload, content_type = await get_bytes(url, self._headers(), self.settings.scraper.timeout_seconds)
                except Exception:
                    logger.exception("auction_source_fetch_failed", extra={"event_name": "auction_source_fetch_failed", "source": self.source})
                    continue
                for candidate in self._parse_payload(payload, content_type, url):
                    if candidate.name in seen:
                        continue
                    seen.add(candidate.name)
                    candidates.append(candidate)
                    if len(candidates) >= self.settings.scoring.max_domains_per_cycle:
                        return candidates
            return candidates

        return await run_resilient(
            self.source,
            fetch,
            policy=RetryPolicy(attempts=2, base_delay_seconds=2.0, timeout_seconds=self.settings.scraper.timeout_seconds),
        )

    def _parse_payload(self, payload: bytes, content_type: str, url: str) -> list[DomainCandidate]:
        lower_type = content_type.lower()
        lower_url = url.lower()
        text_payloads = iter_zip_texts(payload)
        candidates: list[DomainCandidate] = []
        for _, text in text_payloads:
            if "json" in lower_type or lower_url.endswith(".json"):
                candidates.extend(candidates_from_json_payload(text, self.source))
            elif "html" in lower_type and not lower_url.endswith((".csv", ".zip")):
                candidates.extend(candidates_from_html_links(text, url, self.source))
            else:
                candidates.extend(candidates_from_csv_text(text, self.source))
        return candidates

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.scraper.user_agent,
            "Accept": "text/csv,application/json,application/zip,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }


class GoDaddyAuctionsScraper(CsvAuctionScraper):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, settings.scraper.godaddy_auctions_urls, "godaddy_auctions")


class NameJetScraper(CsvAuctionScraper):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, settings.scraper.namejet_urls, "namejet_expiring")


class SnapNamesScraper(CsvAuctionScraper):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, settings.scraper.snapnames_urls, "snapnames")


class DropCatchScraper(CsvAuctionScraper):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, [settings.scraper.dropcatch_expiring_url], "dropcatch")

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self.settings.dropcatch_api_key:
            headers["Authorization"] = f"Bearer {self.settings.dropcatch_api_key.get_secret_value()}"
        return headers
