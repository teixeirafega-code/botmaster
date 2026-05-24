from __future__ import annotations

import random
import re
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient
from app.models import DomainCandidate

DOMAIN_RE = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b",
    re.IGNORECASE,
)


class ExpiredDomainsScraper:
    source = "expireddomains_deleted"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def scrape(self) -> list[DomainCandidate]:
        async def fetch() -> list[DomainCandidate]:
            headers = self._headers()
            timeout = aiohttp.ClientTimeout(total=self.settings.scraper.timeout_seconds)
            candidates: list[DomainCandidate] = []
            seen: set[str] = set()
            url = self.settings.scraper.expireddomains_url
            async with aiohttp.ClientSession(headers=headers) as session:
                for _ in range(self.settings.scraper.expireddomains_max_pages):
                    async with session.get(url, timeout=timeout) as resp:
                        resp.raise_for_status()
                        html = await resp.text()
                    for candidate in self.parse(html):
                        if candidate.name in seen:
                            continue
                        seen.add(candidate.name)
                        candidates.append(candidate)
                        if len(candidates) >= self.settings.scoring.max_domains_per_cycle:
                            return candidates
                    next_url = self.next_page_url(html, url)
                    if not next_url:
                        break
                    url = next_url
            return candidates

        return await run_resilient(
            "expireddomains",
            fetch,
            policy=RetryPolicy(attempts=2, base_delay_seconds=2.0, timeout_seconds=self.settings.scraper.timeout_seconds),
        )

    def _headers(self) -> dict[str, str]:
        user_agents = self.settings.scraper.user_agents or [self.settings.scraper.user_agent]
        origin = self._origin(self.settings.scraper.expireddomains_url)
        return {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": self.settings.scraper.expireddomains_url,
            "Origin": origin.rstrip("/"),
            "Upgrade-Insecure-Requests": "1",
        }

    def _origin(self, url: str) -> str:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return "https://www.expireddomains.net/"
        return f"{parts.scheme}://{parts.netloc}/"

    def parse(self, html: str) -> list[DomainCandidate]:
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        candidates: list[DomainCandidate] = []

        result_nodes = soup.select("td.field_domain, td.domain")
        if not result_nodes:
            result_nodes = soup.select("table.base1 tr td:first-child, table tr td:first-child")
        for node in result_nodes:
            domain = self._normalize_domain(node.get_text(" ", strip=True))
            if not domain:
                continue
            if domain not in seen:
                seen.add(domain)
                candidates.append(DomainCandidate(name=domain, source=self.source))

        return candidates[: self.settings.scoring.max_domains_per_cycle]

    def next_page_url(self, html: str, current_url: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True).lower()
            if "next" in text and "page" in text:
                href = anchor.get("href")
                if isinstance(href, str):
                    return urljoin(current_url, href)
        return ""

    def _normalize_domain(self, value: str) -> str:
        candidate = value.split()[0].lower() if value.split() else ""
        match = DOMAIN_RE.fullmatch(candidate)
        if not match:
            return ""
        domain = match.group(0).strip(".")
        if ".." in domain or domain.startswith("-") or ".-" in domain or "-." in domain:
            return ""
        return domain
