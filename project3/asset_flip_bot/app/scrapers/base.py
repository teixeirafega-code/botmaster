from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from app.config.settings import MarketplaceSettings
from app.models import AssetType, MarketplaceListing
from app.utils.logger import get_logger
from app.utils.retry import retry


MONEY_RE = re.compile(
    r"(?P<currency>US\$|USD|EUR|GBP|\$)?\s*"
    r"(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)"
    r"(?:\s*(?P<suffix>[kKmMbB])\b)?"
)


def money_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    text = html.unescape(str(value)).strip()
    if not text or text.lower() in {"na", "n/a", "none", "contact seller", "undisclosed"}:
        return 0.0
    match = MONEY_RE.search(" ".join(text.split()))
    if not match:
        return 0.0
    number = float(match.group("number").replace(",", ""))
    suffix = (match.group("suffix") or "").lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    elif suffix == "b":
        number *= 1_000_000_000
    return round(number, 2)


def number_to_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    return int(money_to_float(value))


def age_to_months(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    text = str(value).lower()
    years = 0.0
    months = 0.0
    year_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(year|yr|y)", text)
    month_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(month|mo|m)", text)
    if year_match:
        years = float(year_match.group(1))
    if month_match:
        months = float(month_match.group(1))
    if years or months:
        return int(years * 12 + months)
    date_match = re.search(r"(19|20)\d{2}", text)
    if date_match:
        year = int(date_match.group(0))
        return max(0, (datetime.now(timezone.utc).year - year) * 12)
    return 0


def infer_asset_type(*values: Any) -> AssetType:
    text = " ".join(str(value) for value in values if value).lower()
    if "youtube" in text or "channel" in text:
        return AssetType.YOUTUBE
    if "ios" in text or "android" in text or "mobile app" in text or text == "app":
        return AssetType.APP
    if "shopify" in text or "amazon fba" in text or "ecommerce" in text or "e-commerce" in text:
        return AssetType.ECOMMERCE
    if "saas" in text or "software" in text:
        return AssetType.SAAS
    if "newsletter" in text:
        return AssetType.NEWSLETTER
    if "website" in text or "content" in text or "blog" in text:
        return AssetType.WEBSITE
    return AssetType.WEBSITE


def _first_value(data: dict[str, Any], candidates: Iterable[str]) -> Any:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for candidate in candidates:
        if candidate.lower() in lowered:
            value = lowered[candidate.lower()]
            if value not in (None, "", [], {}):
                return value
    return None


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "title", "label", "value", "text"):
            if key in value:
                return _text_value(value[key])
        return ""
    if isinstance(value, list):
        return " ".join(_text_value(item) for item in value if item)
    return html.unescape(str(value)).strip()


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


class ScriptJSONParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._attrs: dict[str, str] = {}
        self._chunks: list[str] = []
        self.scripts: list[tuple[dict[str, str], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._in_script = True
            self._attrs = {key.lower(): value or "" for key, value in attrs}
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.scripts.append((self._attrs, "".join(self._chunks).strip()))
            self._in_script = False
            self._attrs = {}
            self._chunks = []


class LinkTextParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self._current_href: str | None = None
        self._chunks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attrs_map = {key.lower(): value or "" for key, value in attrs}
            href = attrs_map.get("href")
            if href:
                self._current_href = urljoin(self.base_url, href)
                self._chunks = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.text_parts.append(text)
        if self._current_href is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href is not None:
            title = " ".join("".join(self._chunks).split())
            if title:
                self.links.append((self._current_href, html.unescape(title)))
            self._current_href = None
            self._chunks = []


class MarketplaceScraper:
    marketplace_name = "marketplace"

    def __init__(self, settings: MarketplaceSettings) -> None:
        self.settings = settings
        self.logger = get_logger(f"scraper.{self.marketplace_name}")
        self._last_request = 0.0

    def scrape(self) -> list[MarketplaceListing]:
        listings: dict[str, MarketplaceListing] = {}
        for url in self.settings.urls:
            try:
                html_body = self.fetch(url)
                for listing in self.parse(url, html_body):
                    listings[listing.stable_key] = listing
            except Exception as exc:
                self.logger.exception("Failed scraping %s: %s", url, exc)
        return list(listings.values())

    @retry(attempts=3, base_delay=2.0, handled_exceptions=(HTTPError, URLError, TimeoutError))
    def fetch(self, url: str) -> str:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.settings.min_delay_seconds:
            time.sleep(self.settings.min_delay_seconds - elapsed)
        headers = {
            "User-Agent": (
                "AssetFlipBot/1.0 (+https://github.com/asset-flip-bot; "
                "marketplace monitoring)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        cookie = os.getenv(self.settings.cookie_env, "")
        if cookie:
            headers["Cookie"] = cookie
        request = Request(url, headers=headers)
        self.logger.info("Fetching %s", url)
        with urlopen(request, timeout=self.settings.timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
        self._last_request = time.monotonic()
        return body

    def parse(self, page_url: str, html_body: str) -> list[MarketplaceListing]:
        listings = self._parse_json_sources(page_url, html_body)
        if not listings:
            listings = self._parse_html_fallback(page_url, html_body)
        self.logger.info("Parsed %s listings from %s", len(listings), page_url)
        return listings

    def _parse_json_sources(self, page_url: str, html_body: str) -> list[MarketplaceListing]:
        parser = ScriptJSONParser()
        parser.feed(html_body)
        parsed_objects: list[Any] = []
        for attrs, script in parser.scripts:
            if not script:
                continue
            script_type = attrs.get("type", "").lower()
            script_id = attrs.get("id", "").lower()
            if "json" not in script_type and script_id not in {"__next_data__", "ng-state"}:
                continue
            try:
                parsed_objects.append(json.loads(html.unescape(script)))
            except json.JSONDecodeError:
                continue

        listings: dict[str, MarketplaceListing] = {}
        for payload in parsed_objects:
            for candidate in _walk_json(payload):
                listing = self._listing_from_mapping(page_url, candidate)
                if listing:
                    listings[listing.stable_key] = listing
        return list(listings.values())

    def _parse_html_fallback(self, page_url: str, html_body: str) -> list[MarketplaceListing]:
        parser = LinkTextParser(page_url)
        parser.feed(html_body)
        text = " ".join(parser.text_parts)
        links = [
            (url, title)
            for url, title in parser.links
            if self._is_marketplace_link(page_url, url) and len(title) >= 4
        ]

        listings: dict[str, MarketplaceListing] = {}
        for idx, (url, title) in enumerate(links[:200]):
            window_start = max(0, text.lower().find(title.lower()) - 400)
            window = text[window_start : window_start + 900]
            price = self._extract_labeled_money(
                window,
                ["asking price", "price", "listing price", "sale price"],
            )
            revenue = self._extract_labeled_money(
                window,
                ["monthly revenue", "revenue", "monthly net profit", "profit"],
            )
            if not price:
                price = self._first_large_money(window)
            if not price:
                continue
            listing = MarketplaceListing(
                marketplace=self.marketplace_name,
                external_id=self._external_id(url or f"{page_url}:{idx}"),
                name=title,
                url=url,
                asset_type=infer_asset_type(title, window),
                asking_price=price,
                monthly_revenue=revenue,
                monthly_profit=revenue,
                age_months=age_to_months(window),
                monthly_traffic=self._extract_labeled_number(
                    window,
                    ["monthly traffic", "pageviews", "visits", "sessions"],
                ),
                niche=self._extract_niche(window),
                raw={"source": "html_fallback"},
            )
            listings[listing.stable_key] = listing
        return list(listings.values())

    def _listing_from_mapping(
        self,
        page_url: str,
        data: dict[str, Any],
    ) -> MarketplaceListing | None:
        title = _text_value(
            _first_value(
                data,
                [
                    "name",
                    "title",
                    "headline",
                    "businessName",
                    "listingName",
                    "displayName",
                    "shortDescription",
                ],
            )
        )
        price = money_to_float(
            _first_value(
                data,
                [
                    "askingPrice",
                    "asking_price",
                    "price",
                    "salePrice",
                    "sale_price",
                    "listingPrice",
                    "listing_price",
                    "valuation",
                    "amount",
                ],
            )
        )
        revenue = money_to_float(
            _first_value(
                data,
                [
                    "monthlyRevenue",
                    "monthly_revenue",
                    "monthlyNetRevenue",
                    "monthly_net_revenue",
                    "revenueMonthly",
                    "revenue_monthly",
                    "averageMonthlyRevenue",
                    "avgMonthlyRevenue",
                    "grossRevenue",
                    "revenue",
                ],
            )
        )
        profit = money_to_float(
            _first_value(
                data,
                [
                    "monthlyProfit",
                    "monthly_profit",
                    "monthlyNetProfit",
                    "monthly_net_profit",
                    "netProfit",
                    "net_profit",
                    "profit",
                    "averageMonthlyProfit",
                    "avgMonthlyProfit",
                    "cashflow",
                ],
            )
        )
        if not title or price <= 0:
            return None
        raw_url = _text_value(
            _first_value(data, ["url", "canonicalUrl", "permalink", "publicUrl", "listingUrl", "href"])
        )
        listing_url = urljoin(page_url, raw_url) if raw_url else page_url
        external_id = _text_value(_first_value(data, ["id", "_id", "uuid", "slug", "listingId"]))
        if not external_id:
            external_id = self._external_id(listing_url + title)

        niche = _text_value(
            _first_value(
                data,
                ["niche", "category", "industry", "vertical", "market", "monetization", "sector"],
            )
        ) or "unknown"
        asset_type_text = _text_value(
            _first_value(data, ["assetType", "asset_type", "propertyType", "type", "@type"])
        )
        traffic = number_to_int(
            _first_value(
                data,
                [
                    "monthlyTraffic",
                    "monthly_traffic",
                    "monthlyPageviews",
                    "pageviews",
                    "visits",
                    "sessions",
                    "traffic",
                ],
            )
        )
        age_months = age_to_months(
            _first_value(
                data,
                [
                    "ageMonths",
                    "age_months",
                    "age",
                    "established",
                    "founded",
                    "foundedDate",
                    "startDate",
                ],
            )
        )

        return MarketplaceListing(
            marketplace=self.marketplace_name,
            external_id=str(external_id),
            name=title[:240],
            url=listing_url,
            asset_type=infer_asset_type(asset_type_text, title, niche),
            asking_price=price,
            monthly_revenue=revenue,
            monthly_profit=profit,
            age_months=age_months,
            monthly_traffic=traffic,
            niche=niche[:120],
            raw=self._compact_raw(data),
        )

    def _extract_labeled_money(self, text: str, labels: list[str]) -> float:
        lowered = text.lower()
        for label in labels:
            pos = lowered.find(label)
            if pos == -1:
                continue
            value = money_to_float(text[pos : pos + 120])
            if value:
                return value
        return 0.0

    def _extract_labeled_number(self, text: str, labels: list[str]) -> int:
        lowered = text.lower()
        for label in labels:
            pos = lowered.find(label)
            if pos == -1:
                continue
            value = number_to_int(text[pos : pos + 120])
            if value:
                return value
        return 0

    def _first_large_money(self, text: str) -> float:
        values = [money_to_float(match.group(0)) for match in MONEY_RE.finditer(text)]
        values = [value for value in values if value >= 1_000]
        return values[0] if values else 0.0

    def _extract_niche(self, text: str) -> str:
        lowered = text.lower()
        for label in ["niche", "category", "industry"]:
            pos = lowered.find(label)
            if pos != -1:
                snippet = text[pos : pos + 80]
                parts = re.split(r"[:|•-]", snippet, maxsplit=1)
                if len(parts) == 2:
                    value = " ".join(parts[1].split()[:5]).strip()
                    if value:
                        return value
        return "unknown"

    def _is_marketplace_link(self, page_url: str, url: str) -> bool:
        page_host = urlparse(page_url).netloc
        url_host = urlparse(url).netloc
        return not url_host or page_host == url_host

    def _external_id(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _compact_raw(self, data: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str | int | float | bool) or value is None:
                compact[str(key)] = value
            elif isinstance(value, dict) and len(compact) < 30:
                compact[str(key)] = {
                    str(child_key): child_value
                    for child_key, child_value in value.items()
                    if isinstance(child_value, str | int | float | bool) or child_value is None
                }
            if len(compact) >= 40:
                break
        return compact
