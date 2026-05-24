from __future__ import annotations

from app.config.settings import MarketplaceSettings
from app.scrapers.base import MarketplaceScraper


class AcquireComScraper(MarketplaceScraper):
    marketplace_name = "acquirecom"


def build_scraper(settings: MarketplaceSettings) -> AcquireComScraper:
    return AcquireComScraper(settings)

