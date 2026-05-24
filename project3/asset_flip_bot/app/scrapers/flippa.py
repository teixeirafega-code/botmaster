from __future__ import annotations

from app.config.settings import MarketplaceSettings
from app.scrapers.base import MarketplaceScraper


class FlippaScraper(MarketplaceScraper):
    marketplace_name = "flippa"


def build_scraper(settings: MarketplaceSettings) -> FlippaScraper:
    return FlippaScraper(settings)

