from __future__ import annotations

import logging
import re
from typing import Any

import requests

from app.models import TrendTopic
from app.monitors.base import TrendMonitor
from app.utils.retry import retry
from app.utils.text import clean_text


LOGGER = logging.getLogger(__name__)


class TikTokHashtagMonitor(TrendMonitor):
    source_name = "tiktok"

    CREATIVE_CENTER_URL = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
    DISCOVER_URL = "https://www.tiktok.com/discover"

    def __init__(self, config: dict):
        super().__init__(config)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.get(
                    "user_agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
                ),
                "Accept": "application/json,text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def fetch(self) -> list[TrendTopic]:
        topics = self._fetch_creative_center()
        if topics:
            return topics
        LOGGER.info("TikTok Creative Center did not return hashtags; trying discover page")
        return self._fetch_discover_page()

    @retry(attempts=2, delay_seconds=1.0, exceptions=(requests.RequestException,))
    def _fetch_creative_center(self) -> list[TrendTopic]:
        country = self.config.get("country_code", "US")
        limit = int(self.config.get("limit", 20))
        period = int(self.config.get("period_days", 7))
        response = self.session.get(
            self.CREATIVE_CENTER_URL,
            params={
                "period": period,
                "limit": limit,
                "page": 1,
                "country_code": country,
                "order_by": "popular",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            LOGGER.warning(
                "TikTok Creative Center returned HTTP %s",
                response.status_code,
            )
            return []

        try:
            payload = response.json()
        except ValueError:
            return []

        raw_items = self._extract_creative_items(payload)
        topics: list[TrendTopic] = []
        for rank, item in enumerate(raw_items[:limit], start=1):
            hashtag = self._get_first(item, ["hashtag_name", "hashtag", "name", "keyword"])
            if not hashtag:
                continue
            title = "#" + clean_text(str(hashtag)).lstrip("#")
            views = int(float(self._get_first(item, ["view_count", "views", "video_views"]) or 0))
            rank_score = max(1, limit - rank + 1) * 20
            score = rank_score + min(500, views / 100_000)
            topics.append(
                TrendTopic(
                    source=self.source_name,
                    title=title,
                    url=f"https://www.tiktok.com/tag/{title.lstrip('#')}",
                    score=round(score, 2),
                    volume=views,
                    engagement=score,
                    hashtags=[title],
                    raw={"rank": rank, "creative_center": item},
                )
            )
        return topics

    @retry(attempts=2, delay_seconds=1.0, exceptions=(requests.RequestException,))
    def _fetch_discover_page(self) -> list[TrendTopic]:
        response = self.session.get(self.DISCOVER_URL, timeout=20)
        response.raise_for_status()
        html = response.text
        names = set(re.findall(r'"challengeName"\s*:\s*"([^"]+)"', html))
        names.update(re.findall(r"#([A-Za-z0-9_]{3,40})", html))
        seeds = [str(tag).lstrip("#") for tag in self.config.get("seed_hashtags", [])]
        names.update(seeds)
        limit = int(self.config.get("limit", 20))
        topics: list[TrendTopic] = []
        for rank, name in enumerate(sorted(names)[:limit], start=1):
            cleaned = clean_text(name).replace(" ", "")
            if len(cleaned) < 3:
                continue
            title = "#" + cleaned.lstrip("#")
            score = max(1, limit - rank + 1) * 12
            topics.append(
                TrendTopic(
                    source=self.source_name,
                    title=title,
                    url=f"https://www.tiktok.com/tag/{cleaned}",
                    score=score,
                    volume=score,
                    engagement=score,
                    hashtags=[title],
                    raw={"rank": rank, "source": "discover_or_seed"},
                )
            )
        return topics

    def _extract_creative_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[Any] = [
            payload.get("data", {}).get("list") if isinstance(payload.get("data"), dict) else None,
            payload.get("data", {}).get("hashtags") if isinstance(payload.get("data"), dict) else None,
            payload.get("list"),
            payload.get("hashtags"),
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    def _get_first(self, item: dict[str, Any], names: list[str]) -> Any:
        for name in names:
            if item.get(name) not in (None, ""):
                return item[name]
        return None
