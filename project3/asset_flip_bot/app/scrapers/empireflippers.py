from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from app.config.settings import MarketplaceSettings
from app.models import AssetType, MarketplaceListing
from app.scrapers.base import age_to_months, infer_asset_type, money_to_float, number_to_int
from app.scrapers.base import MarketplaceScraper


class EmpireFlippersScraper(MarketplaceScraper):
    marketplace_name = "empireflippers"
    api_url = "https://api.empireflippers.com/api/v1/listings/list"
    min_asking_price = 1_000.0
    max_asking_price = 10_000_000.0
    api_page_limit = 100
    max_api_pages = 20

    def scrape(self) -> list[MarketplaceListing]:
        try:
            listings = self._scrape_api()
            if listings:
                return listings
        except Exception as exc:
            self.logger.exception("Empire Flippers API scrape failed: %s", exc)
        return self._realistic_listings(super().scrape())

    def parse(self, page_url: str, html_body: str) -> list[MarketplaceListing]:
        payload = self._json_payload(html_body)
        if payload is not None:
            return self._parse_api_payload(payload)
        return self._realistic_listings(super().parse(page_url, html_body))

    def _scrape_api(self) -> list[MarketplaceListing]:
        listings: dict[str, MarketplaceListing] = {}
        for page in range(1, self.max_api_pages + 1):
            query = urlencode(
                {
                    "page": page,
                    "limit": self.api_page_limit,
                    "listing_status": "For Sale",
                }
            )
            body = self.fetch(f"{self.api_url}?{query}")
            page_listings = self._parse_api_payload(json.loads(body))
            if not page_listings:
                break
            for listing in page_listings:
                listings[listing.stable_key] = listing
            if len(page_listings) < self.api_page_limit:
                break
        self.logger.info("Parsed %s Empire Flippers listings from API", len(listings))
        return list(listings.values())

    def _parse_api_payload(self, payload: dict[str, Any]) -> list[MarketplaceListing]:
        raw_listings = self._raw_api_listings(payload)
        listings: list[MarketplaceListing] = []
        for raw in raw_listings:
            listing = self._listing_from_api(raw)
            if listing:
                listings.append(listing)
        return listings

    def _raw_api_listings(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", payload)
        if isinstance(data, dict):
            listings = data.get("listings", [])
        else:
            listings = data
        if not isinstance(listings, list):
            return []
        return [item for item in listings if isinstance(item, dict)]

    def _listing_from_api(self, raw: dict[str, Any]) -> MarketplaceListing | None:
        if raw.get("unpriced") is True:
            return None

        asking_price = money_to_float(raw.get("listing_price"))
        if not self._valid_asking_price(asking_price):
            self.logger.debug(
                "Skipping Empire Flippers listing %s with out-of-range price %s",
                raw.get("listing_number") or raw.get("id"),
                asking_price,
            )
            return None

        listing_number = raw.get("listing_number") or raw.get("id") or ""
        external_id = str(raw.get("id") or listing_number)
        name = str(raw.get("public_title") or f"Empire Flippers Listing #{listing_number}")
        monthly_profit = money_to_float(raw.get("average_monthly_net_profit"))
        monthly_revenue = money_to_float(raw.get("average_monthly_gross_revenue"))
        monetizations = self._names(raw.get("monetizations"), "monetization")
        niches = self._names(raw.get("niches"), "niche")
        sites = raw.get("sites") if isinstance(raw.get("sites"), list) else []
        site_platforms = [
            str(site.get("platform"))
            for site in sites
            if isinstance(site, dict) and site.get("platform")
        ]

        return MarketplaceListing(
            marketplace=self.marketplace_name,
            external_id=external_id,
            name=name[:240],
            url=self._listing_url(listing_number),
            asset_type=self._asset_type(name, monetizations, site_platforms),
            asking_price=asking_price,
            monthly_revenue=monthly_revenue,
            monthly_profit=monthly_profit,
            age_months=self._age_months(raw.get("first_made_money_at")),
            monthly_traffic=self._traffic(raw),
            niche=", ".join(niches or monetizations or ["unknown"])[:120],
            raw=self._compact_raw(raw),
        )

    def _json_payload(self, text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped.startswith("{"):
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _valid_asking_price(self, value: float) -> bool:
        return self.min_asking_price <= value <= self.max_asking_price

    def _realistic_listings(
        self,
        listings: list[MarketplaceListing],
    ) -> list[MarketplaceListing]:
        return [
            listing
            for listing in listings
            if self._valid_asking_price(listing.asking_price)
        ]

    def _listing_url(self, listing_number: object) -> str:
        if listing_number:
            return f"https://empireflippers.com/listing/{listing_number}/"
        return "https://empireflippers.com/marketplace/"

    def _names(self, value: Any, key: str) -> list[str]:
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                raw_name = item.get(key) or item.get("name")
            else:
                raw_name = item
            if raw_name:
                names.append(str(raw_name))
        return names

    def _asset_type(
        self,
        title: str,
        monetizations: list[str],
        site_platforms: list[str],
    ) -> AssetType:
        text = " ".join([title, *monetizations, *site_platforms]).lower()
        if "youtube" in text:
            return AssetType.YOUTUBE
        if "mobile app" in text or "ios" in text or "android" in text:
            return AssetType.APP
        if "saas" in text or "software" in text:
            return AssetType.SAAS
        if (
            "amazon fba" in text
            or "ecommerce" in text
            or "e-commerce" in text
            or "shopify" in text
            or "woocommerce" in text
        ):
            return AssetType.ECOMMERCE
        if "newsletter" in text:
            return AssetType.NEWSLETTER
        if (
            "affiliate" in text
            or "display advertising" in text
            or "adsense" in text
            or "content" in text
            or "website" in text
        ):
            return AssetType.WEBSITE
        if "agency" in text or "service" in text:
            return AssetType.OTHER
        return infer_asset_type(text)

    def _age_months(self, value: Any) -> int:
        if not value:
            return 0
        if isinstance(value, str):
            try:
                founded = datetime.fromisoformat(value.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                return max(0, (now.year - founded.year) * 12 + now.month - founded.month)
            except ValueError:
                pass
        return age_to_months(value)

    def _traffic(self, raw: dict[str, Any]) -> int:
        combined = raw.get("combined_site_metrics")
        if isinstance(combined, list) and combined:
            latest = combined[-1]
            if isinstance(latest, dict):
                users = number_to_int(latest.get("unique_users"))
                page_views = number_to_int(latest.get("page_views"))
                if users or page_views:
                    return max(users, page_views)

        sites = raw.get("sites")
        if not isinstance(sites, list):
            return 0
        traffic = 0
        for site in sites:
            if not isinstance(site, dict):
                continue
            traffic = max(
                traffic,
                number_to_int(site.get("average_monthly_unique_users")),
                number_to_int(site.get("average_monthly_page_views")),
            )
        return traffic


def build_scraper(settings: MarketplaceSettings) -> EmpireFlippersScraper:
    return EmpireFlippersScraper(settings)
