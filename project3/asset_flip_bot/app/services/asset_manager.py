from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.analyzers.opportunity_scorer import OpportunityScorer
from app.analyzers.revenue_multiplier import RevenueMultiplier
from app.analyzers.valuation_engine import ValuationEngine
from app.config.settings import AppSettings, MarketplaceSettings
from app.models import MarketplaceListing, ScoredOpportunity
from app.notifications.telegram import TelegramNotifier
from app.scrapers.acquirecom import AcquireComScraper
from app.scrapers.empireflippers import EmpireFlippersScraper
from app.scrapers.flippa import FlippaScraper
from app.services.profit_tracker import ProfitTracker
from app.utils.logger import get_logger


class Scraper(Protocol):
    settings: MarketplaceSettings

    def scrape(self) -> list[MarketplaceListing]:
        ...


@dataclass(slots=True)
class ScanSummary:
    assets_monitored: int
    opportunities_found: int
    alerted: int
    total_potential_profit: float
    opportunities: list[ScoredOpportunity]


SCRAPER_CLASSES = {
    "flippa": FlippaScraper,
    "empireflippers": EmpireFlippersScraper,
    "acquirecom": AcquireComScraper,
}


class AssetManager:
    def __init__(
        self,
        settings: AppSettings,
        scrapers: list[Scraper] | None = None,
        notifier: TelegramNotifier | None = None,
        profit_tracker: ProfitTracker | None = None,
    ) -> None:
        self.settings = settings
        self.logger = get_logger("services.asset_manager")
        self.scrapers = scrapers if scrapers is not None else self._build_scrapers()
        multiplier = RevenueMultiplier(settings.multipliers)
        self.valuation_engine = ValuationEngine(multiplier)
        self.scorer = OpportunityScorer(
            undervalued_threshold=settings.undervalued_threshold,
            niche_bonus=settings.niche_bonus,
        )
        self.notifier = notifier or TelegramNotifier(settings.telegram, settings.paper_mode)
        self.profit_tracker = profit_tracker or ProfitTracker(settings.stats_path)
        self.state_path = Path(settings.state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def scan_once(self) -> ScanSummary:
        listings = self.scrape_all()
        opportunities = self.analyze(listings)
        state = self._load_state()
        alerted_keys: set[str] = set(state.get("alerted_keys", []))
        newly_alerted = 0

        for opportunity in opportunities:
            alert_key = self.profit_tracker.alert_key(opportunity)
            if alert_key in alerted_keys:
                continue
            sent = self.notifier.send_opportunity(opportunity)
            if sent:
                alerted_keys.add(alert_key)
                newly_alerted += 1

        stats = self.profit_tracker.record_scan(len(listings), opportunities)
        self._save_state(listings, alerted_keys)

        total_profit = sum(item.valuation.profit_potential for item in opportunities)
        self.logger.info(
            "Scan finished: %s assets, %s opportunities, %s alerts",
            len(listings),
            len(opportunities),
            newly_alerted,
        )
        return ScanSummary(
            assets_monitored=len(listings),
            opportunities_found=len(opportunities),
            alerted=newly_alerted,
            total_potential_profit=round(total_profit, 2),
            opportunities=opportunities,
        )

    def scrape_all(self) -> list[MarketplaceListing]:
        listings: dict[str, MarketplaceListing] = {}
        for scraper in self.scrapers:
            if not scraper.settings.enabled:
                self.logger.info("Skipping disabled marketplace %s", scraper.settings.name)
                continue
            for listing in scraper.scrape():
                listings[listing.stable_key] = listing
        return list(listings.values())

    def analyze(self, listings: list[MarketplaceListing]) -> list[ScoredOpportunity]:
        opportunities: list[ScoredOpportunity] = []
        for listing in listings:
            if listing.asking_price <= 0:
                continue
            valuation = self.valuation_engine.estimate(listing)
            scored = self.scorer.score(listing, valuation)
            if (
                scored.is_undervalued
                and scored.opportunity_score >= self.settings.min_score_alert
            ):
                opportunities.append(scored)
        opportunities.sort(
            key=lambda item: (item.opportunity_score, item.valuation.profit_potential),
            reverse=True,
        )
        return opportunities

    def _build_scrapers(self) -> list[Scraper]:
        scrapers: list[Scraper] = []
        for marketplace in self.settings.marketplaces:
            scraper_class = SCRAPER_CLASSES.get(marketplace.name.lower())
            if scraper_class is None:
                self.logger.warning("No scraper registered for marketplace %s", marketplace.name)
                continue
            scrapers.append(scraper_class(marketplace))
        return scrapers

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {"alerted_keys": [], "latest_listings": []}
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            self.logger.warning("State file is unreadable. Starting with empty state.")
            return {"alerted_keys": [], "latest_listings": []}

    def _save_state(
        self,
        listings: list[MarketplaceListing],
        alerted_keys: set[str],
    ) -> None:
        payload = {
            "alerted_keys": sorted(alerted_keys),
            "latest_listings": [listing.to_dict() for listing in listings],
        }
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        temp_path.replace(self.state_path)

