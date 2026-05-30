from __future__ import annotations

import logging
import random
from collections.abc import Iterable
from urllib.parse import urlsplit

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient
from app.domain_sources import (
    candidates_from_csv_text,
    candidates_from_html_links,
    candidates_from_json_payload,
    iter_zip_texts,
)
from app.models import DomainCandidate
from app.scrapers.expireddomains import ExpiredDomainsScraper

logger = logging.getLogger(__name__)
AUCTION_REQUEST_TIMEOUT_SECONDS = 8
MAX_AUCTION_FEED_BYTES = 5_000_000


class CsvAuctionScraper:
    source = "auction_csv"

    def __init__(self, settings: Settings, urls: Iterable[str], source: str, fallback: ExpiredDomainsScraper | None = None) -> None:
        self.settings = settings
        self.urls = tuple(urls)
        self.source = source
        self.fallback = fallback or ExpiredDomainsScraper(settings)

    async def scrape(self) -> list[DomainCandidate]:
        async def fetch() -> list[DomainCandidate]:
            candidates: list[DomainCandidate] = []
            seen: set[str] = set()
            for url in self.urls:
                try:
                    payload, content_type = await self._fetch_url(url)
                except Exception as exc:
                    logger.warning(
                        "auction_source_fetch_failed",
                        extra={
                            "event_name": "auction_source_fetch_failed",
                            "source": self.source,
                            "url": url,
                            "error": str(exc)[:200],
                        },
                    )
                    continue
                for candidate in self._parse_payload(payload, content_type, url):
                    if candidate.name in seen:
                        continue
                    seen.add(candidate.name)
                    candidates.append(candidate)
                    if len(candidates) >= self.settings.scoring.max_domains_per_cycle:
                        return candidates
            if candidates:
                return candidates
            return await self._fallback_candidates()

        return await run_resilient(
            self.source,
            fetch,
            policy=RetryPolicy(
                attempts=1,
                base_delay_seconds=1.0,
                timeout_seconds=(AUCTION_REQUEST_TIMEOUT_SECONDS * max(1, len(self.urls))) + self.settings.scraper.timeout_seconds,
            ),
        )

    async def _fetch_url(self, url: str) -> tuple[bytes, str]:
        timeout = aiohttp.ClientTimeout(
            total=min(self.settings.scraper.timeout_seconds, AUCTION_REQUEST_TIMEOUT_SECONDS),
            connect=5,
            sock_read=5,
        )
        async with aiohttp.ClientSession(headers=self._headers(url), timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                content_length = int(resp.headers.get("Content-Length") or 0)
                if content_length > MAX_AUCTION_FEED_BYTES:
                    raise RuntimeError(f"auction feed too large: {content_length} bytes")
                payload = bytearray()
                async for chunk in resp.content.iter_chunked(65_536):
                    payload.extend(chunk)
                    if len(payload) > MAX_AUCTION_FEED_BYTES:
                        raise RuntimeError(f"auction feed exceeded {MAX_AUCTION_FEED_BYTES} bytes")
                return bytes(payload), resp.headers.get("Content-Type", "")

    async def _fallback_candidates(self) -> list[DomainCandidate]:
        logger.warning(
            "auction_source_fallback_expireddomains",
            extra={"event_name": "auction_source_fallback_expireddomains", "source": self.source},
        )
        candidates = await self.fallback.scrape()
        for candidate in candidates:
            candidate.source_metadata["fallback_for"] = self.source
        return candidates

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

    def _headers(self, url: str = "") -> dict[str, str]:
        user_agents = self.settings.scraper.user_agents or [self.settings.scraper.user_agent]
        user_agent = random.choice(user_agents)
        origin = self._origin(url)
        return {
            "User-Agent": user_agent,
            "Accept": "text/csv,application/json,application/zip,application/octet-stream,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": origin,
            "Origin": origin.rstrip("/"),
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        }

    def _origin(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/"
        return "https://www.expireddomains.net/"


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

    def _headers(self, url: str = "") -> dict[str, str]:
        headers = super()._headers(url or self.settings.scraper.dropcatch_expiring_url)
        if self.settings.dropcatch_api_key:
            headers["Authorization"] = f"Bearer {self.settings.dropcatch_api_key.get_secret_value()}"
        return headers
