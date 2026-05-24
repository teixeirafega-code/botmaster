from pathlib import Path

from app.config.settings import (
    AppSettings,
    DomainSettings,
    MonitorSettings,
    Settings,
    TelegramSettings,
)
from app.models import TrendSignal
from app.services.trend_manager import TrendManager


class FakeMonitor:
    platform = "fake"

    def collect(self) -> list[TrendSignal]:
        return [
            TrendSignal(
                name="AI launch tool",
                platform="fake",
                growth_velocity=250,
                search_volume=100,
                social_engagement=9000,
                keywords=("ai", "tool", "launch"),
            )
        ]


def test_manager_persists_cycle_and_dashboard(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path,
        app=AppSettings(
            paper_mode=True,
            trend_score_threshold=60,
            state_db_path=tmp_path / "trend_hunter.db",
            log_file=tmp_path / "trend_hunter.log",
        ),
        monitors=MonitorSettings(),
        domains=DomainSettings(enabled=False),
        telegram=TelegramSettings(enabled=False),
    )
    manager = TrendManager(settings, monitors=[FakeMonitor()])

    summary = manager.run_cycle()
    snapshot = manager.dashboard_snapshot()

    assert summary.signals_collected == 1
    assert summary.trends_scored == 1
    assert snapshot["trends_detected_today"] == 1
    assert snapshot["top_trending_now"][0]["name"] == "AI launch tool"

