from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.models import TrendTopic
from app.monitors import GoogleTrendsMonitor, RedditMonitor, TikTokHashtagMonitor
from app.monitors.base import TrendMonitor


LOGGER = logging.getLogger(__name__)


class TrendService:
    def __init__(self, config: dict):
        self.config = config
        self.monitors = self._build_monitors()

    def collect(self) -> list[TrendTopic]:
        if not self.monitors:
            LOGGER.warning("No trend monitors are enabled")
            return []

        topics: list[TrendTopic] = []
        workers = min(len(self.monitors), int(self.config.get("trends", {}).get("max_workers", 3)))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(monitor.fetch): monitor for monitor in self.monitors}
            for future in as_completed(futures):
                monitor = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    LOGGER.warning("%s monitor failed: %s", monitor.source_name, exc)
                    continue
                LOGGER.info("%s monitor returned %s topics", monitor.source_name, len(result))
                topics.extend(result)
        return topics

    def _build_monitors(self) -> list[TrendMonitor]:
        trends_config = self.config.get("trends", {})
        monitors: list[TrendMonitor] = []

        reddit_config = trends_config.get("reddit", {})
        if reddit_config.get("enabled", True):
            monitors.append(RedditMonitor(reddit_config))

        google_config = trends_config.get("google_trends", {})
        if google_config.get("enabled", True):
            monitors.append(GoogleTrendsMonitor(google_config))

        tiktok_config = trends_config.get("tiktok", {})
        provider = str(tiktok_config.get("provider", "disabled")).lower()
        if tiktok_config.get("enabled", False) and provider not in {"", "disabled", "none"}:
            monitors.append(TikTokHashtagMonitor(tiktok_config))
        elif tiktok_config.get("enabled", False):
            LOGGER.warning("TikTok monitor requested but no provider is configured")

        return monitors
