from __future__ import annotations

from statistics import mean
from typing import Any

from app.config.settings import GoogleTrendsSettings
from app.models import TrendSignal
from app.utils.logger import get_logger
from app.utils.retry import retry


logger = get_logger(__name__)


class GoogleTrendsMonitor:
    platform = "google_trends"

    def __init__(self, settings: GoogleTrendsSettings) -> None:
        self.settings = settings

    def collect(self) -> list[TrendSignal]:
        if not self.settings.enabled:
            return []
        try:
            from pytrends.request import TrendReq
        except ImportError:
            logger.warning("pytrends is not installed; Google Trends monitor skipped")
            return []

        try:
            client = TrendReq(
                hl=self.settings.hl,
                tz=self.settings.tz,
                timeout=(5, self.settings.timeout_seconds),
            )
            topics = self._fetch_trending_topics(client)
            if not topics:
                return []
            interest = self._fetch_interest(client, topics[: self.settings.max_results])
            signals = []
            for topic in topics[: self.settings.max_results]:
                metrics = interest.get(topic, {})
                signals.append(
                    TrendSignal(
                        name=topic,
                        platform=self.platform,
                        growth_velocity=float(metrics.get("growth_velocity", 100.0)),
                        search_volume=float(metrics.get("search_volume", 70.0)),
                        social_engagement=0.0,
                        keywords=(topic,),
                        metadata={"source": "pytrends.trending_searches", **metrics},
                    )
                )
            return signals
        except Exception as exc:
            logger.exception("Google Trends collection failed: %s", exc)
            return []

    @retry(max_attempts=3, initial_delay_seconds=1.0, max_delay_seconds=10.0)
    def _fetch_trending_topics(self, client: Any) -> list[str]:
        frame = client.trending_searches(pn=self.settings.pn)
        if frame is None or frame.empty:
            return []
        topics = [str(value).strip() for value in frame.iloc[:, 0].dropna().tolist()]
        return [topic for topic in topics if topic]

    def _fetch_interest(self, client: Any, topics: list[str]) -> dict[str, dict[str, float]]:
        metrics: dict[str, dict[str, float]] = {}
        for chunk in _chunks(topics, 5):
            try:
                client.build_payload(chunk, timeframe=self.settings.timeframe, geo=self.settings.geo)
                frame = client.interest_over_time()
            except Exception as exc:
                logger.warning("Google Trends interest lookup failed for %s: %s", ", ".join(chunk), exc)
                continue
            if frame is None or frame.empty:
                continue
            for topic in chunk:
                if topic not in frame:
                    continue
                series = [float(value) for value in frame[topic].dropna().tolist()]
                if not series:
                    continue
                latest = series[-1]
                baseline_values = series[:-3] or series[:-1] or [latest]
                baseline = max(mean(baseline_values), 1.0)
                growth_velocity = max(0.0, (latest - baseline) / baseline * 100.0)
                metrics[topic] = {
                    "search_volume": max(0.0, min(100.0, latest)),
                    "growth_velocity": max(0.0, growth_velocity),
                    "baseline_interest": round(baseline, 2),
                    "latest_interest": round(latest, 2),
                }
        return metrics


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
