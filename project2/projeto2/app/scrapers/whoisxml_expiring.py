from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
import tarfile
import zipfile
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient
from app.models import DomainCandidate

logger = logging.getLogger(__name__)

DOMAIN_RE = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b",
    re.IGNORECASE,
)
DOMAIN_KEYS = ("domain", "domainname", "domainnames", "name")
CREATED_KEYS = ("created", "createddate", "creationdate")
EVENT_KEYS = ("event", "eventtype", "type", "action", "operation", "status", "reason")
EXPIRING_EVENT_MARKERS = ("drop", "dropped", "delete", "deleted", "expire", "expired", "released")


class WhoisXmlExpiringDomainsScraper:
    source = "whoisxml_expiring"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def scrape(self) -> list[DomainCandidate]:
        async def fetch() -> list[DomainCandidate]:
            timeout = aiohttp.ClientTimeout(total=self.settings.scraper.timeout_seconds)
            headers = self._headers()
            async with aiohttp.ClientSession(headers=headers) as session:
                urls = list(self.settings.scraper.whoisxml_download_urls)
                if not urls:
                    html = await self._get_text(session, self.settings.scraper.whoisxml_url, timeout)
                    urls = self.discover_download_urls(html, self.settings.scraper.whoisxml_url)

                candidates: list[DomainCandidate] = []
                seen: set[str] = set()
                for url in urls[: self.settings.scraper.whoisxml_download_limit]:
                    payload, content_type = await self._get_bytes(session, url, timeout)
                    for candidate in self.parse_feed(payload, content_type=content_type, source_url=url):
                        if candidate.name in seen:
                            continue
                        seen.add(candidate.name)
                        candidates.append(candidate)
                        if len(candidates) >= self.settings.scoring.max_domains_per_cycle:
                            return candidates
                return candidates

        return await run_resilient(
            "whoisxml_expiring",
            fetch,
            policy=RetryPolicy(attempts=2, base_delay_seconds=2.0, timeout_seconds=self.settings.scraper.timeout_seconds),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.scraper.user_agent,
            "Accept": "text/csv,application/json,application/zip,application/gzip,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    async def _get_text(self, session: aiohttp.ClientSession, url: str, client_timeout: aiohttp.ClientTimeout) -> str:
        async with session.get(url, timeout=client_timeout) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def _get_bytes(self, session: aiohttp.ClientSession, url: str, client_timeout: aiohttp.ClientTimeout) -> tuple[bytes, str]:
        async with session.get(url, timeout=client_timeout) as resp:
            resp.raise_for_status()
            return await resp.read(), resp.headers.get("Content-Type", "")

    def discover_download_urls(self, html: str, base_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        scored_urls: list[tuple[int, str]] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            url = urljoin(base_url, href)
            text = anchor.get_text(" ", strip=True).lower()
            context = self._anchor_context(anchor).lower()
            haystack = f"{url} {text} {context}"
            if "download" not in haystack and "sample" not in haystack:
                continue

            score = 0
            if "csv" in haystack or "json" in haystack or "zip" in haystack or "datafeeds" in haystack:
                score += 10
            if any(marker in haystack for marker in ("dropped", "expired", "just expired")):
                score += 30
            if any(plan in haystack for plan in ("basic", "professional", "enterprise", "ultimate")):
                score += 15
            if "newly registered domains only" in haystack:
                score -= 40
            if score <= 0:
                continue
            scored_urls.append((score, url))

        seen: set[str] = set()
        urls: list[str] = []
        for _, url in sorted(scored_urls, key=lambda item: item[0], reverse=True):
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def _anchor_context(self, anchor: Any) -> str:
        parent = anchor.find_parent(["li", "tr", "section", "div", "article"]) or anchor.parent
        if parent is None:
            return str(anchor.get_text(" ", strip=True))
        return str(parent.get_text(" ", strip=True))

    def parse_feed(self, payload: bytes | str, *, content_type: str = "", source_url: str = "") -> list[DomainCandidate]:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        candidates: list[DomainCandidate] = []
        seen: set[str] = set()
        for text, text_url in self._iter_text_payloads(data, content_type=content_type, source_url=source_url):
            for candidate in self.parse_text(text, source_url=text_url):
                if candidate.name in seen:
                    continue
                seen.add(candidate.name)
                candidates.append(candidate)
                if len(candidates) >= self.settings.scoring.max_domains_per_cycle:
                    return candidates
        return candidates

    def _iter_text_payloads(self, data: bytes, *, content_type: str, source_url: str) -> list[tuple[str, str]]:
        lower_url = source_url.lower()
        lower_type = content_type.lower()
        if zipfile.is_zipfile(io.BytesIO(data)):
            texts: list[tuple[str, str]] = []
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    with archive.open(name) as fh:
                        texts.extend(self._iter_text_payloads(fh.read(), content_type="", source_url=name))
            return texts
        tar_texts = self._extract_tar_payload(data)
        if tar_texts:
            return tar_texts
        if lower_url.endswith(".gz") or "gzip" in lower_type:
            return [(gzip.decompress(data).decode("utf-8", errors="replace"), source_url[:-3])]
        return [(data.decode("utf-8", errors="replace"), source_url)]

    def _extract_tar_payload(self, data: bytes) -> list[tuple[str, str]]:
        texts: list[tuple[str, str]] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    texts.extend(self._iter_text_payloads(extracted.read(), content_type="", source_url=member.name))
        except tarfile.TarError:
            return []
        return texts

    def parse_text(self, text: str, *, source_url: str = "") -> list[DomainCandidate]:
        stripped = text.lstrip("\ufeff \r\n\t")
        if not stripped:
            return []
        if stripped[0] in "[{":
            return self._parse_json(stripped)
        return self._parse_csv_or_lines(stripped, source_url=source_url)

    def _parse_json(self, text: str) -> list[DomainCandidate]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("whoisxml_json_parse_failed", extra={"event_name": "whoisxml_json_parse_failed"})
            return []

        rows: list[dict[str, Any]] = []
        self._collect_json_rows(payload, rows)
        return self._candidates_from_rows(rows)

    def _collect_json_rows(self, value: Any, rows: list[dict[str, Any]]) -> None:
        if isinstance(value, list):
            for item in value:
                self._collect_json_rows(item, rows)
            return
        if not isinstance(value, dict):
            return
        if self._domain_from_row(value):
            rows.append(value)
        for item in value.values():
            if isinstance(item, (list, dict)):
                self._collect_json_rows(item, rows)

    def _parse_csv_or_lines(self, text: str, *, source_url: str) -> list[DomainCandidate]:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return []

        dialect = self._sniff_dialect("\n".join(lines[:5]))
        first_fields = [self._clean_key(part) for part in next(csv.reader([lines[0]], dialect=dialect))]
        has_header = any(field in DOMAIN_KEYS for field in first_fields) or any(field in EVENT_KEYS for field in first_fields)
        if has_header:
            rows = [dict(row) for row in csv.DictReader(lines, dialect=dialect)]
            return self._candidates_from_rows(rows)

        inferred_tld = self._tld_from_url(source_url)
        rows = []
        for row in csv.reader(lines, dialect=dialect):
            if not row:
                continue
            name = row[0].strip()
            if inferred_tld and "." not in name:
                name = f"{name}.{inferred_tld}"
            rows.append({"domain": name})
        return self._candidates_from_rows(rows)

    def _sniff_dialect(self, sample: str) -> type[csv.Dialect] | csv.Dialect:
        try:
            return csv.Sniffer().sniff(sample)
        except csv.Error:
            return csv.excel

    def _candidates_from_rows(self, rows: list[dict[str, Any]]) -> list[DomainCandidate]:
        seen: set[str] = set()
        candidates: list[DomainCandidate] = []
        for row in rows:
            if not self._row_matches_expiring_feed(row):
                continue
            domain = self._domain_from_row(row)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            candidates.append(DomainCandidate(name=domain, source=self.source, age_years=self._age_years(row)))
        return candidates[: self.settings.scoring.max_domains_per_cycle]

    def _row_matches_expiring_feed(self, row: dict[str, Any]) -> bool:
        event_values = [str(value).lower() for key, value in row.items() if self._clean_key(key) in EVENT_KEYS and value]
        if not event_values:
            return True
        return any(any(marker in value for marker in EXPIRING_EVENT_MARKERS) for value in event_values)

    def _domain_from_row(self, row: dict[str, Any]) -> str:
        for key, value in row.items():
            clean_key = self._clean_key(key)
            if clean_key not in DOMAIN_KEYS:
                continue
            domain = self._normalize_domain(str(value))
            if domain:
                return domain
        for value in row.values():
            if isinstance(value, str):
                domain = self._normalize_domain(value)
                if domain:
                    return domain
        return ""

    def _normalize_domain(self, value: str) -> str:
        match = DOMAIN_RE.search(value.strip().lower())
        if not match:
            return ""
        domain = match.group(0).strip(".")
        if ".." in domain or domain.startswith("-") or ".-" in domain or "-." in domain:
            return ""
        return domain

    def _age_years(self, row: dict[str, Any]) -> int:
        for key, value in row.items():
            if self._clean_key(key) in CREATED_KEYS:
                created_at = self._parse_date(str(value))
                if created_at:
                    return max(0, (datetime.now(UTC).date() - created_at.date()).days // 365)
        return 0

    def _parse_date(self, value: str) -> datetime | None:
        value = value.strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    def _tld_from_url(self, url: str) -> str:
        filename = urlsplit(url).path.rsplit("/", 1)[-1].lower()
        match = re.search(r"(?:add|drop|dropped)\.([a-z0-9-]+)\.csv", filename)
        if match:
            return match.group(1)
        path_parts = [part for part in urlsplit(url).path.lower().split("/") if part]
        for part in reversed(path_parts[:-1]):
            if re.fullmatch(r"[a-z][a-z0-9-]{1,62}", part) and part not in {"datafeeds", "newly_registered_domains"}:
                return part
        return ""

    def _clean_key(self, key: Any) -> str:
        return str(key).strip().lower().replace(" ", "").replace("-", "").replace(".", "").replace("_", "")
