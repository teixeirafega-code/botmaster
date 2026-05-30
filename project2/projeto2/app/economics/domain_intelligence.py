from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Any

import aiohttp

from app.config.settings import Settings
from app.domain_sources import parse_datetime
from app.economics.models import ComparableSale
from app.models import DomainCandidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaybackHistory:
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    capture_count: int = 0
    status_200_count: int = 0

    @property
    def age_years(self) -> int:
        if not self.first_seen:
            return 0
        return max(0, (datetime.now(UTC).date() - self.first_seen.date()).days // 365)

    @property
    def history_score(self) -> float:
        if self.capture_count <= 0:
            return 0.0
        return min(1.0, (self.status_200_count / max(1, self.capture_count)) * min(1.0, self.capture_count / 120))


@dataclass(frozen=True)
class DomainIntelligence:
    wayback: WaybackHistory = field(default_factory=WaybackHistory)
    namebio_sales: tuple[ComparableSale, ...] = ()
    tld_average_price: float = 0.0
    keyword_sales_average: float = 0.0
    backlink_count: int = 0

    @property
    def comparable_median(self) -> float:
        prices = [sale.sale_price for sale in self.namebio_sales]
        if prices:
            return float(median(prices))
        return max(self.tld_average_price, self.keyword_sales_average, 0.0)


class DomainIntelligenceClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def enrich(self, candidate: DomainCandidate) -> DomainIntelligence:
        if self.settings.paper_mode:
            intelligence = self._paper_intelligence(candidate)
        else:
            async with aiohttp.ClientSession() as session:
                wayback = await self.wayback_history(candidate.name, session)
                namebio_sales = await self.namebio_comps(candidate.name, session)
                tld_average = await self.namebio_tld_average(candidate.name, session)
                keyword_average = await self.namebio_keyword_average(candidate.name, session)
                backlinks = await self.backlink_count(candidate.name, session)
            intelligence = DomainIntelligence(
                wayback=wayback,
                namebio_sales=tuple(namebio_sales),
                tld_average_price=tld_average,
                keyword_sales_average=keyword_average,
                backlink_count=backlinks,
            )

        candidate.age_years = max(candidate.age_years, intelligence.wayback.age_years)
        candidate.backlinks = max(candidate.backlinks, intelligence.backlink_count)
        candidate.source_metadata.update(
            {
                "wayback_capture_count": intelligence.wayback.capture_count,
                "wayback_first_seen": intelligence.wayback.first_seen.isoformat() if intelligence.wayback.first_seen else "",
                "namebio_comparable_count": len(intelligence.namebio_sales),
                "namebio_comparable_median": round(intelligence.comparable_median, 2),
                "namebio_tld_average_price": round(intelligence.tld_average_price, 2),
                "namebio_keyword_average_price": round(intelligence.keyword_sales_average, 2),
            }
        )
        return intelligence

    async def wayback_history(self, domain: str, session: aiohttp.ClientSession) -> WaybackHistory:
        params = {
            "url": domain,
            "output": "json",
            "fl": "timestamp,statuscode",
            "collapse": "digest",
            "filter": "statuscode:200",
            "limit": "5000",
        }
        try:
            async with session.get("https://web.archive.org/cdx", params=params, timeout=self.settings.scraper.timeout_seconds) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception:
            logger.exception("wayback_lookup_failed", extra={"event_name": "wayback_lookup_failed", "domain": domain})
            return WaybackHistory()
        if not isinstance(rows, list) or len(rows) <= 1:
            return WaybackHistory()
        timestamps = [self._wayback_timestamp(row[0]) for row in rows[1:] if isinstance(row, list) and row]
        timestamps = [item for item in timestamps if item]
        if not timestamps:
            return WaybackHistory()
        status_200 = sum(1 for row in rows[1:] if len(row) > 1 and str(row[1]) == "200")
        return WaybackHistory(first_seen=min(timestamps), last_seen=max(timestamps), capture_count=len(timestamps), status_200_count=status_200)

    async def namebio_comps(self, domain: str, session: aiohttp.ClientSession) -> list[ComparableSale]:
        if not self.settings.namebio_email or not self.settings.namebio_api_key:
            return []
        payload = {
            "email": self.settings.namebio_email,
            "key": self.settings.namebio_api_key.get_secret_value(),
            "domain": domain,
            "order_by": "price",
            "order_dir": "desc",
            "months_old": 60,
        }
        data = await self._post_form(session, f"{self.settings.namebio_base_url.rstrip('/')}/comps/", payload)
        sales_payload = data.get("sales", []) if isinstance(data, dict) else []
        sales: list[ComparableSale] = []
        for row in sales_payload:
            sale = self._namebio_sale(row, domain)
            if sale:
                sales.append(sale)
        return sales

    async def namebio_tld_average(self, domain: str, session: aiohttp.ClientSession) -> float:
        extension = "." + domain.rsplit(".", 1)[-1].lower()
        data = await self._post_form(session, f"{self.settings.namebio_base_url.rstrip('/')}/tldstats", {"extension": extension})
        if not isinstance(data, dict):
            return 0.0
        stats = data.get("data", {})
        if isinstance(stats, dict):
            period = stats.get("1y") or stats.get("all")
            if isinstance(period, dict):
                return float(period.get("price_avg") or 0.0)
        return 0.0

    async def namebio_keyword_average(self, domain: str, session: aiohttp.ClientSession) -> float:
        if not self.settings.namebio_email or not self.settings.namebio_api_key:
            return 0.0
        keyword = domain.rsplit(".", 1)[0]
        payload = {
            "email": self.settings.namebio_email,
            "key": self.settings.namebio_api_key.get_secret_value(),
            "keyword": keyword,
            "months_old": 60,
        }
        data = await self._post_form(session, f"{self.settings.namebio_base_url.rstrip('/')}/keywordstats/", payload)
        stats = data.get("stats", data) if isinstance(data, dict) else {}
        return float(stats.get("price_avg") or stats.get("average_price") or 0.0) if isinstance(stats, dict) else 0.0

    async def backlink_count(self, domain: str, session: aiohttp.ClientSession) -> int:
        url = self.settings.backlink_proxy_url.format(domain=domain)
        try:
            async with session.get(url, timeout=self.settings.scraper.timeout_seconds) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "").lower()
                if "json" in content_type:
                    return self._count_backlinks(await resp.json())
                text = await resp.text()
        except Exception:
            logger.exception("backlink_proxy_lookup_failed", extra={"event_name": "backlink_proxy_lookup_failed", "domain": domain})
            return 0
        return self._count_backlink_text(text)

    async def _post_form(self, session: aiohttp.ClientSession, url: str, payload: dict[str, object]) -> dict[str, Any]:
        try:
            async with session.post(url, data=payload, timeout=self.settings.scraper.timeout_seconds) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
                return data
        except Exception:
            logger.exception("domain_intelligence_post_failed", extra={"event_name": "domain_intelligence_post_failed", "url": url})
            return {}

    def _paper_intelligence(self, candidate: DomainCandidate) -> DomainIntelligence:
        digest = hashlib.sha256(f"intel:{candidate.name}".encode()).hexdigest()
        capture_count = int(digest[:4], 16) % 360
        backlinks = int(digest[4:8], 16) % 900
        age_years = int(digest[8:10], 16) % 18
        first_seen = datetime(datetime.now(UTC).year - age_years, 1, 1, tzinfo=UTC) if age_years else None
        median_price = 350 + (int(digest[10:14], 16) % 4500)
        stem, extension = candidate.name.rsplit(".", 1)
        sale = ComparableSale(
            stem + "." + extension,
            float(median_price),
            datetime.now(UTC),
            marketplace="paper_namebio",
            niche="paper",
            extension=f".{extension}",
        )
        return DomainIntelligence(
            wayback=WaybackHistory(first_seen=first_seen, last_seen=datetime.now(UTC), capture_count=capture_count, status_200_count=int(capture_count * 0.8)),
            namebio_sales=(sale,),
            tld_average_price=float(median_price),
            keyword_sales_average=float(median_price * 0.8),
            backlink_count=backlinks,
        )

    def _wayback_timestamp(self, value: object) -> datetime | None:
        text = str(value)
        if len(text) < 8:
            return None
        return parse_datetime(f"{text[:4]}-{text[4:6]}-{text[6:8]}T{text[8:10] or '00'}:{text[10:12] or '00'}:{text[12:14] or '00'}Z")

    def _namebio_sale(self, row: object, domain: str) -> ComparableSale | None:
        extension = "." + domain.rsplit(".", 1)[-1].lower()
        if isinstance(row, dict):
            sale_domain = str(row.get("domain") or domain).lower()
            price = float(row.get("price") or row.get("sale_price") or 0)
            sold_at = parse_datetime(row.get("date") or row.get("sold_at")) or datetime.now(UTC)
            marketplace = str(row.get("venue") or row.get("marketplace") or "namebio")
            return ComparableSale(sale_domain, price, sold_at, marketplace=marketplace, extension=extension) if price > 0 else None
        if isinstance(row, list) and len(row) >= 3:
            if len(row) >= 4 and isinstance(row[0], str) and "." in row[0]:
                sale_domain = row[0].lower()
                price_value = row[1]
                date_value = row[2]
                marketplace = str(row[3])
            else:
                sale_domain = domain
                price_value = row[0]
                date_value = row[1]
                marketplace = str(row[2])
            price = float(str(price_value).replace(",", ""))
            sold_at = parse_datetime(date_value) or datetime.now(UTC)
            return ComparableSale(sale_domain, price, sold_at, marketplace=marketplace, extension=extension)
        return None

    def _count_backlinks(self, payload: object) -> int:
        if isinstance(payload, dict):
            for key in ("backlinks", "backlink_count", "total", "count", "referring_domains"):
                if key in payload:
                    return int(float(payload[key] or 0))
            return max((self._count_backlinks(value) for value in payload.values()), default=0)
        if isinstance(payload, list):
            return len(payload)
        return 0

    def _count_backlink_text(self, text: str) -> int:
        lines = [line for line in text.splitlines() if line.strip() and not line.lower().startswith(("error", "limit"))]
        return len(lines)
