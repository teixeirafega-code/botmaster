from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

import requests

from app.config.settings import TikTokSettings
from app.models import TrendSignal
from app.utils.logger import get_logger


logger = get_logger(__name__)


class TikTokMonitor:
    platform = "tiktok"

    def __init__(self, settings: TikTokSettings) -> None:
        self.settings = settings

    def collect(self) -> list[TrendSignal]:
        if not self.settings.enabled:
            return []

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        signals: list[TrendSignal] = []
        for hashtag in self.settings.hashtags:
            signal = self._collect_hashtag(session, hashtag)
            if signal:
                signals.append(signal)
        return signals

    def _collect_hashtag(self, session: requests.Session, hashtag: str) -> TrendSignal | None:
        tag = _clean_hashtag(hashtag)
        if not tag:
            return None

        url = f"https://www.tiktok.com/tag/{quote(tag)}"
        try:
            response = session.get(url, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("TikTok hashtag page failed for #%s: %s", tag, exc)
            return None

        metadata = self._extract_page_metadata(response.text, tag)
        view_count = metadata["view_count"]
        engagement = min(float(view_count), 10_000_000.0) if view_count > 0 else 0.0
        growth_velocity = min(max(view_count / 1_000_000.0, 0.0), 250.0) if view_count > 0 else 0.0
        search_volume = min(max(view_count / 10_000_000.0, 0.0), 100.0) if view_count > 0 else 0.0

        return TrendSignal(
            name=f"#{tag}",
            platform=self.platform,
            growth_velocity=growth_velocity,
            search_volume=search_volume,
            social_engagement=engagement,
            url=url,
            keywords=tuple(metadata["related_hashtags"]),
            metadata={
                "source": "tiktok_hashtag_page",
                "description": metadata["description"],
                "title": metadata["title"],
                "view_count": view_count,
            },
        )

    def _extract_page_metadata(self, page: str, tag: str) -> dict[str, Any]:
        descriptions = [
            _first_group(page, r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']'),
            _first_group(page, r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']'),
        ]
        description = html.unescape(next((value for value in descriptions if value), "")).strip()
        title = html.unescape(_first_group(page, r"<title>(.*?)</title>") or f"#{tag} on TikTok").strip()
        text = html.unescape(" ".join([description, title, page[:200_000]]))

        numbers = []
        for pattern in (
            r'"viewCount"\s*:\s*"?([0-9][0-9,.]*)"?',
            r'"playCount"\s*:\s*"?([0-9][0-9,.]*)"?',
            r"([0-9][0-9,.]*\s*[KMB]?)\s+views?",
            r"([0-9][0-9,.]*\s*[KMB]?)\s+posts?",
        ):
            numbers.extend(_number_from_text(match) for match in re.findall(pattern, text, flags=re.IGNORECASE))
        view_count = max(numbers) if numbers else 0

        related = [f"#{tag}"]
        related.extend(f"#{item}" for item in re.findall(r"#([A-Za-z][A-Za-z0-9_]{1,40})", text))
        return {
            "title": title,
            "description": description,
            "view_count": view_count,
            "related_hashtags": list(dict.fromkeys(related))[:12],
        }


def _clean_hashtag(value: str) -> str:
    match = re.search(r"[A-Za-z0-9_]+", value.lstrip("#").lower())
    return match.group(0) if match else ""


def _first_group(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _number_from_text(value: str) -> int:
    text = value.strip().replace(",", "")
    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.lower().endswith("b"):
        multiplier = 1_000_000_000
        text = text[:-1]
    try:
        return int(float(text.strip()) * multiplier)
    except ValueError:
        return 0
