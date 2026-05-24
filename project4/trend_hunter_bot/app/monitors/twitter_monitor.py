from __future__ import annotations

import email.utils
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests

from app.config.settings import TwitterSettings
from app.models import TrendSignal
from app.utils.logger import get_logger


logger = get_logger(__name__)


class TwitterMonitor:
    platform = "twitter"

    def __init__(self, settings: TwitterSettings) -> None:
        self.settings = settings

    def collect(self) -> list[TrendSignal]:
        if not self.settings.enabled:
            return []

        signals = self._collect_with_ntscraper()
        if signals:
            return signals
        return self._collect_with_nitter_rss()

    def _collect_with_ntscraper(self) -> list[TrendSignal]:
        try:
            from ntscraper import Nitter
        except ImportError:
            logger.warning("ntscraper is not installed; Twitter monitor using Nitter RSS fallback")
            return []

        try:
            scraper = Nitter(log_level=0, skip_instance_check=False)
        except TypeError:
            try:
                scraper = Nitter()
            except Exception as exc:
                logger.warning("ntscraper initialization failed; Twitter monitor using Nitter RSS fallback: %s", exc)
                return []
        except Exception as exc:
            logger.warning("ntscraper initialization failed; Twitter monitor using Nitter RSS fallback: %s", exc)
            return []

        signals: list[TrendSignal] = []
        for query in self.settings.queries:
            try:
                payload = scraper.get_tweets(
                    query,
                    mode="term",
                    number=self.settings.max_results_per_query,
                )
                tweets = payload.get("tweets", []) if isinstance(payload, dict) else []
                for tweet in tweets[: self.settings.max_results_per_query]:
                    signal = self._signal_from_ntscraper_tweet(tweet, query)
                    if signal:
                        signals.append(signal)
            except Exception as exc:
                logger.warning("ntscraper query failed for %r: %s", query, exc)
        return signals

    def _signal_from_ntscraper_tweet(self, tweet: dict[str, Any], query: str) -> TrendSignal | None:
        text = str(tweet.get("text") or tweet.get("content") or "")
        if not text:
            return None

        hashtags = tuple(re.findall(r"#\w+", text))
        stats = tweet.get("stats") if isinstance(tweet.get("stats"), dict) else {}
        like_count = _to_int(stats.get("likes") or tweet.get("likes"))
        retweet_count = _to_int(stats.get("retweets") or tweet.get("retweets"))
        reply_count = _to_int(stats.get("comments") or stats.get("replies") or tweet.get("replies"))
        quote_count = _to_int(stats.get("quotes") or tweet.get("quotes"))
        engagement = like_count + retweet_count * 2 + reply_count + quote_count
        if engagement < self.settings.min_engagement:
            return None

        created = _parse_tweet_date(tweet.get("date") or tweet.get("published"))
        age_hours = self._age_hours(created)
        if age_hours > self.settings.since_hours:
            return None

        name = hashtags[0] if hashtags else self._topic_from_text(text, query)
        return TrendSignal(
            name=name,
            platform=self.platform,
            growth_velocity=engagement / max(age_hours, 0.25),
            search_volume=0.0,
            social_engagement=float(engagement),
            url=str(tweet.get("link") or tweet.get("url") or ""),
            keywords=(*hashtags, *_keywords(text), query),
            metadata={
                "query": query,
                "source": "ntscraper",
                "likes": like_count,
                "retweets": retweet_count,
                "replies": reply_count,
                "quotes": quote_count,
                "age_hours": round(age_hours, 2),
            },
        )

    def _collect_with_nitter_rss(self) -> list[TrendSignal]:
        session = requests.Session()
        signals: list[TrendSignal] = []
        for query in self.settings.queries:
            encoded = quote_plus(query)
            for instance in self.settings.nitter_instances:
                url = f"{instance.rstrip('/')}/search/rss?f=tweets&q={encoded}"
                try:
                    response = session.get(url, timeout=15, headers={"User-Agent": "TrendHunterBot/1.0"})
                    response.raise_for_status()
                    signals.extend(self._signals_from_rss(response.text, query, url))
                    break
                except requests.RequestException as exc:
                    logger.warning("Nitter RSS failed for %s via %s: %s", query, instance, exc)
        return signals

    def _signals_from_rss(self, xml_text: str, query: str, source_url: str) -> list[TrendSignal]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return []

        signals: list[TrendSignal] = []
        items = root.findall(".//item")[: self.settings.max_results_per_query]
        for item in items:
            title = item.findtext("title", default=query)
            link = item.findtext("link", default=source_url)
            published = item.findtext("pubDate")
            created = _parse_rss_date(published)
            age_hours = self._age_hours(created)
            name = self._topic_from_text(title, query)
            signals.append(
                TrendSignal(
                    name=name,
                    platform=self.platform,
                    growth_velocity=25.0 / max(age_hours, 0.25),
                    search_volume=0.0,
                    social_engagement=25.0,
                    url=link,
                    keywords=(_keywords(title) + (query,)),
                    metadata={"query": query, "source": "nitter_rss", "age_hours": round(age_hours, 2)},
                )
            )
        return signals

    def _age_hours(self, created: datetime | None) -> float:
        if created is None:
            return float(self.settings.since_hours)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.01)

    def _topic_from_text(self, text: str, fallback: str) -> str:
        hashtags = re.findall(r"#\w+", text)
        if hashtags:
            return hashtags[0]
        words = _keywords(text, limit=4)
        return " ".join(words[:3]).title() if words else fallback


def _parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return _ensure_utc(parsed)


def _parse_tweet_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not value:
        return None
    text = str(value).strip()
    for parser in (
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        email.utils.parsedate_to_datetime,
    ):
        try:
            return _ensure_utc(parser(text))
        except (TypeError, ValueError):
            continue
    for fmt in ("%b %d, %Y · %I:%M %p UTC", "%b %d, %Y · %H:%M UTC", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "")
    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def _keywords(text: str, limit: int = 10) -> tuple[str, ...]:
    stop_words = {
        "https",
        "http",
        "with",
        "that",
        "this",
        "from",
        "they",
        "have",
        "about",
        "just",
        "into",
        "your",
        "will",
        "what",
    }
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text)
    clean = [token.lower() for token in tokens if token.lower() not in stop_words]
    return tuple(dict.fromkeys(clean[:limit]))
