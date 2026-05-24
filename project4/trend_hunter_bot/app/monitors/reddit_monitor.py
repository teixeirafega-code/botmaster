from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from app.config.settings import RedditSettings
from app.models import TrendSignal
from app.utils.logger import get_logger


logger = get_logger(__name__)


class RedditMonitor:
    platform = "reddit"

    def __init__(self, settings: RedditSettings) -> None:
        self.settings = settings

    def collect(self) -> list[TrendSignal]:
        if not self.settings.enabled:
            return []

        if self.settings.client_id and self.settings.client_secret:
            praw_signals = self._collect_with_praw()
            if praw_signals:
                return praw_signals

        return self._collect_public_json()

    def _collect_with_praw(self) -> list[TrendSignal]:
        try:
            import praw
        except ImportError:
            logger.warning("praw is not installed; Reddit monitor using public JSON fallback")
            return []

        try:
            reddit = praw.Reddit(
                client_id=self.settings.client_id,
                client_secret=self.settings.client_secret,
                username=self.settings.username,
                password=self.settings.password,
                user_agent=self.settings.user_agent,
            )
            signals: list[TrendSignal] = []
            for subreddit_name in self.settings.subreddits:
                subreddit = reddit.subreddit(subreddit_name)
                for post in subreddit.hot(limit=self.settings.max_posts_per_subreddit):
                    signal = self._signal_from_post(
                        title=str(post.title),
                        subreddit=subreddit_name,
                        score=int(post.score or 0),
                        comments=int(post.num_comments or 0),
                        created_utc=float(post.created_utc),
                        url=f"https://reddit.com{post.permalink}",
                        post_id=str(post.id),
                    )
                    if signal:
                        signals.append(signal)
            return signals
        except Exception as exc:
            logger.exception("Reddit PRAW collection failed: %s", exc)
            return []

    def _collect_public_json(self) -> list[TrendSignal]:
        signals: list[TrendSignal] = []
        session = requests.Session()
        session.headers.update({"User-Agent": self.settings.user_agent})
        for subreddit in self.settings.subreddits:
            url = f"https://www.reddit.com/r/{subreddit}/hot.json"
            try:
                response = session.get(url, params={"limit": self.settings.max_posts_per_subreddit}, timeout=15)
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException as exc:
                logger.warning("Reddit public JSON failed for r/%s: %s", subreddit, exc)
                continue
            except ValueError as exc:
                logger.warning("Reddit returned invalid JSON for r/%s: %s", subreddit, exc)
                continue

            children = payload.get("data", {}).get("children", [])
            for child in children:
                data = child.get("data", {})
                signal = self._signal_from_post(
                    title=str(data.get("title", "")),
                    subreddit=subreddit,
                    score=int(data.get("score") or 0),
                    comments=int(data.get("num_comments") or 0),
                    created_utc=float(data.get("created_utc") or 0),
                    url=f"https://reddit.com{data.get('permalink')}" if data.get("permalink") else None,
                    post_id=str(data.get("id", "")),
                )
                if signal:
                    signals.append(signal)
        return signals

    def _signal_from_post(
        self,
        title: str,
        subreddit: str,
        score: int,
        comments: int,
        created_utc: float,
        url: str | None,
        post_id: str,
    ) -> TrendSignal | None:
        if not title or score < self.settings.min_score:
            return None
        age_hours = self._age_hours(created_utc)
        engagement = float(score + comments * 3)
        velocity = engagement / max(age_hours, 0.25)
        return TrendSignal(
            name=title[:140],
            platform=self.platform,
            growth_velocity=velocity,
            search_volume=0.0,
            social_engagement=engagement,
            url=url,
            keywords=(subreddit, *_keywords(title)),
            metadata={
                "subreddit": subreddit,
                "score": score,
                "comments": comments,
                "age_hours": round(age_hours, 2),
                "post_id": post_id,
            },
        )

    def _age_hours(self, created_utc: float) -> float:
        if created_utc <= 0:
            return 24.0
        created = datetime.fromtimestamp(created_utc, tz=timezone.utc)
        return max((datetime.now(timezone.utc) - created).total_seconds() / 3600.0, 0.01)


def _keywords(text: str, limit: int = 8) -> tuple[str, ...]:
    stop_words = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "what",
        "when",
        "where",
        "will",
        "have",
        "has",
        "are",
        "you",
        "your",
    }
    tokens = [token.strip(".,:;!?()[]{}\"'").lower() for token in text.split()]
    clean = [token for token in tokens if len(token) > 3 and token not in stop_words]
    return tuple(dict.fromkeys(clean[:limit]))

