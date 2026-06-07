from __future__ import annotations

import logging
import time
from typing import Any

import requests

from app.models import TrendTopic
from app.monitors.base import MonitorError, TrendMonitor
from app.utils.retry import retry
from app.utils.text import clean_text


LOGGER = logging.getLogger(__name__)


class RedditMonitor(TrendMonitor):
    source_name = "reddit"

    def __init__(self, config: dict):
        super().__init__(config)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.get(
                    "user_agent",
                    "ShortsMasterBot/1.0 trend research bot",
                )
            }
        )

    def fetch(self) -> list[TrendTopic]:
        topics: list[TrendTopic] = []
        subreddits = self.config.get("subreddits", [])
        limit = int(self.config.get("limit_per_subreddit", 12))
        for subreddit in subreddits:
            try:
                topics.extend(self._fetch_subreddit(str(subreddit), limit))
            except Exception as exc:
                LOGGER.warning("Reddit monitor failed for r/%s: %s", subreddit, exc)
        return topics

    @retry(attempts=3, delay_seconds=1.5, exceptions=(requests.RequestException,))
    def _fetch_subreddit(self, subreddit: str, limit: int) -> list[TrendTopic]:
        url = f"https://www.reddit.com/r/{subreddit}/hot.json"
        response = self.session.get(
            url,
            params={"limit": limit, "raw_json": 1},
            timeout=20,
        )
        if response.status_code == 403:
            LOGGER.warning("Reddit blocked r/%s hot JSON with HTTP 403", subreddit)
            return []
        if response.status_code == 429:
            raise MonitorError("Reddit rate limit reached")
        response.raise_for_status()
        data = response.json()
        children = data.get("data", {}).get("children", [])
        topics: list[TrendTopic] = []
        for child in children:
            post = child.get("data", {})
            topic = self._topic_from_post(post, subreddit)
            if topic:
                topics.append(topic)
        return topics

    def _topic_from_post(self, post: dict[str, Any], subreddit: str) -> TrendTopic | None:
        title = clean_text(str(post.get("title", "")))
        if len(title) < 12 or post.get("stickied"):
            return None
        ups = int(post.get("ups") or 0)
        comments = int(post.get("num_comments") or 0)
        ratio = float(post.get("upvote_ratio") or 0.75)
        created_utc = float(post.get("created_utc") or time.time())
        age_hours = max(1.0, (time.time() - created_utc) / 3600)
        engagement = (ups * ratio) + (comments * 3)
        score = engagement / age_hours
        permalink = post.get("permalink") or ""
        return TrendTopic(
            source=self.source_name,
            title=title,
            url=f"https://www.reddit.com{permalink}" if permalink else "",
            score=round(score, 2),
            volume=ups + comments,
            engagement=round(engagement, 2),
            hashtags=[],
            raw={
                "subreddit": subreddit,
                "ups": ups,
                "comments": comments,
                "upvote_ratio": ratio,
                "created_utc": created_utc,
                "id": post.get("id"),
            },
        )
