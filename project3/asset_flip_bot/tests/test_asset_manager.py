from pathlib import Path

from app.config.settings import AppSettings, MarketplaceSettings, TelegramSettings
from app.models import AssetType, MarketplaceListing, ScoredOpportunity
from app.services.asset_manager import AssetManager


class FakeScraper:
    def __init__(self) -> None:
        self.settings = MarketplaceSettings(name="fake", enabled=True, urls=[])

    def scrape(self) -> list[MarketplaceListing]:
        return [
            MarketplaceListing(
                marketplace="fake",
                external_id="asset-1",
                name="Undervalued SaaS website",
                url="https://example.com/asset-1",
                asset_type=AssetType.SAAS,
                asking_price=20_000,
                monthly_revenue=2_000,
                monthly_profit=2_000,
                age_months=36,
                monthly_traffic=50_000,
                niche="B2B SaaS",
            )
        ]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[ScoredOpportunity] = []

    def send_opportunity(self, opportunity: ScoredOpportunity) -> bool:
        self.sent.append(opportunity)
        return True


def build_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        paper_mode=True,
        scan_interval_minutes=30,
        log_level="INFO",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.json",
        stats_path=tmp_path / "stats.json",
        max_listing_age_days=14,
        min_score_alert=70,
        undervalued_threshold=0.5,
        marketplaces=[],
        multipliers={},
        niche_bonus={"saas": 10},
        telegram=TelegramSettings(enabled=False, bot_token="", chat_id=""),
    )


def test_asset_manager_records_and_deduplicates_alerts(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    manager = AssetManager(
        settings=build_settings(tmp_path),
        scrapers=[FakeScraper()],
        notifier=notifier,  # type: ignore[arg-type]
    )

    first = manager.scan_once()
    second = manager.scan_once()

    assert first.opportunities_found == 1
    assert first.alerted == 1
    assert second.opportunities_found == 1
    assert second.alerted == 0
    assert len(notifier.sent) == 1

