from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.actions.content_generator import ContentGenerator
from app.actions.domain_registrar import DomainRegistrar
from app.analyzers.opportunity_detector import OpportunityDetector
from app.analyzers.trend_scorer import TrendScorer
from app.config.settings import Settings
from app.models import CycleSummary, OpportunityReport, ScoredTrend, TrendSignal, utc_now
from app.monitors.google_trends import GoogleTrendsMonitor
from app.monitors.reddit_monitor import RedditMonitor
from app.monitors.tiktok_monitor import TikTokMonitor
from app.monitors.twitter_monitor import TwitterMonitor
from app.notifications.telegram import TelegramNotifier
from app.utils.logger import get_logger


logger = get_logger(__name__)


class TrendMonitor(Protocol):
    platform: str

    def collect(self) -> list[TrendSignal]:
        ...


class TrendManager:
    def __init__(
        self,
        settings: Settings,
        monitors: Iterable[TrendMonitor] | None = None,
        notifier: TelegramNotifier | None = None,
        opportunity_detector: OpportunityDetector | None = None,
    ) -> None:
        self.settings = settings
        self.scorer = TrendScorer(settings.scoring)
        self.monitors = list(monitors) if monitors is not None else self._build_monitors()
        self.notifier = notifier or TelegramNotifier(settings.telegram, settings.paper_mode)
        self.opportunity_detector = opportunity_detector or OpportunityDetector(
            settings=settings,
            domain_registrar=DomainRegistrar(settings.domains, settings.paper_mode),
            content_generator=ContentGenerator(settings.content),
        )
        self.db_path = settings.app.state_db_path
        self._init_db()

    def run_cycle(self) -> CycleSummary:
        started_at = utc_now()
        errors: list[str] = []
        all_signals: list[TrendSignal] = []
        alerts_sent = 0
        opportunities = 0

        logger.info("Trend collection cycle started with %d monitors", len(self.monitors))
        for monitor in self.monitors:
            try:
                signals = monitor.collect()
                logger.info("%s collected %d signals", monitor.platform, len(signals))
                all_signals.extend(signals)
            except Exception as exc:
                message = f"{monitor.platform} failed: {exc}"
                logger.exception(message)
                errors.append(message)

        self._save_signals(all_signals)
        scored_trends = self.scorer.score_signals(all_signals)
        self._save_trends(scored_trends)

        for trend in scored_trends:
            if trend.score < self.settings.app.trend_score_threshold:
                continue
            if self._was_actioned_recently(trend.normalized_name):
                logger.info("Skipping duplicate opportunity action for %s", trend.name)
                continue

            report = self.opportunity_detector.detect_and_act(trend)
            if report is None:
                continue
            opportunities += 1
            self._save_opportunity(report)
            if self.notifier.send_opportunity_alert(report):
                alerts_sent += 1

        finished_at = utc_now()
        summary = CycleSummary(
            started_at=started_at,
            finished_at=finished_at,
            signals_collected=len(all_signals),
            trends_scored=len(scored_trends),
            opportunities_detected=opportunities,
            alerts_sent=alerts_sent,
            errors=tuple(errors),
        )
        logger.info("Trend collection cycle finished: %s", summary.to_dict())
        return summary

    def dashboard_snapshot(self, top_limit: int = 10) -> dict[str, object]:
        now = utc_now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        last_24h = now - timedelta(hours=24)
        with self._connect() as connection:
            trends_today = connection.execute(
                "SELECT COUNT(DISTINCT normalized_name) FROM trends WHERE observed_at >= ?",
                (start_of_day.isoformat(),),
            ).fetchone()[0]
            domains_registered = connection.execute(
                "SELECT COALESCE(SUM(registered_domain_count), 0) FROM actions WHERE created_at >= ?",
                (start_of_day.isoformat(),),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT name, score, platforms_json, observed_at
                FROM trends
                WHERE observed_at >= ?
                ORDER BY score DESC, observed_at DESC
                LIMIT ?
                """,
                (last_24h.isoformat(), top_limit),
            ).fetchall()

        top_trends = [
            {
                "name": row["name"],
                "score": row["score"],
                "platforms": json.loads(row["platforms_json"]),
                "observed_at": row["observed_at"],
            }
            for row in rows
        ]
        return {
            "trends_detected_today": trends_today,
            "domains_registered_today": domains_registered,
            "top_trending_now": top_trends,
        }

    def _build_monitors(self) -> list[TrendMonitor]:
        return [
            GoogleTrendsMonitor(self.settings.monitors.google_trends),
            RedditMonitor(self.settings.monitors.reddit),
            TwitterMonitor(self.settings.monitors.twitter),
            TikTokMonitor(self.settings.monitors.tiktok),
        ]

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    growth_velocity REAL NOT NULL,
                    search_volume REAL NOT NULL,
                    social_engagement REAL NOT NULL,
                    url TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_signals_observed_at ON signals(observed_at);
                CREATE INDEX IF NOT EXISTS idx_signals_normalized_name ON signals(normalized_name);

                CREATE TABLE IF NOT EXISTS trends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    score REAL NOT NULL,
                    component_scores_json TEXT NOT NULL,
                    platforms_json TEXT NOT NULL,
                    signal_count INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trends_observed_at ON trends(observed_at);
                CREATE INDEX IF NOT EXISTS idx_trends_score ON trends(score);
                CREATE INDEX IF NOT EXISTS idx_trends_normalized_name ON trends(normalized_name);

                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    trend_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    score REAL NOT NULL,
                    mode TEXT NOT NULL,
                    action_taken INTEGER NOT NULL,
                    registered_domain_count INTEGER NOT NULL,
                    domain_actions_json TEXT NOT NULL,
                    social_handles_json TEXT NOT NULL,
                    content_ideas_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at);
                CREATE INDEX IF NOT EXISTS idx_actions_normalized_name ON actions(normalized_name);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _save_signals(self, signals: list[TrendSignal]) -> None:
        if not signals:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO signals (
                    observed_at, name, normalized_name, platform,
                    growth_velocity, search_volume, social_engagement, url, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        signal.observed_at.isoformat(),
                        signal.name,
                        signal.normalized_name,
                        signal.platform,
                        signal.growth_velocity,
                        signal.search_volume,
                        signal.social_engagement,
                        signal.url,
                        json.dumps(
                            {"keywords": signal.keywords, "metadata": signal.metadata},
                            ensure_ascii=True,
                            default=str,
                        ),
                    )
                    for signal in signals
                ],
            )

    def _save_trends(self, trends: list[ScoredTrend]) -> None:
        if not trends:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO trends (
                    observed_at, name, normalized_name, score,
                    component_scores_json, platforms_json, signal_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        trend.detected_at.isoformat(),
                        trend.name,
                        trend.normalized_name,
                        trend.score,
                        json.dumps(trend.component_scores, ensure_ascii=True),
                        json.dumps(trend.platforms, ensure_ascii=True),
                        len(trend.signals),
                    )
                    for trend in trends
                ],
            )

    def _save_opportunity(self, report: OpportunityReport) -> None:
        registered_domain_count = sum(
            1 for action in report.domain_actions if action.action in {"paper_registered", "registered"}
        )
        mode = "paper" if self.settings.paper_mode else "production"
        if report.domain_actions:
            mode = report.domain_actions[0].mode

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO actions (
                    created_at, trend_name, normalized_name, score, mode, action_taken,
                    registered_domain_count, domain_actions_json, social_handles_json, content_ideas_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.created_at.isoformat(),
                    report.trend.name,
                    report.trend.normalized_name,
                    report.trend.score,
                    mode,
                    1 if report.action_taken else 0,
                    registered_domain_count,
                    json.dumps([action.to_dict() for action in report.domain_actions], ensure_ascii=True),
                    json.dumps(report.social_handles, ensure_ascii=True),
                    json.dumps([idea.to_dict() for idea in report.content_ideas], ensure_ascii=True),
                ),
            )

    def _was_actioned_recently(self, normalized_name: str, hours: int = 24) -> bool:
        cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM actions
                WHERE normalized_name = ? AND created_at >= ?
                """,
                (normalized_name, cutoff),
            ).fetchone()
        return bool(row and row[0] > 0)


def ensure_database(settings: Settings) -> Path:
    manager = TrendManager(settings, monitors=[])
    return manager.db_path

