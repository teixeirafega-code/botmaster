from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import requests

from app.models import TrendTopic
from app.monitors.base import MonitorError, TrendMonitor
from app.utils.text import clean_text


LOGGER = logging.getLogger(__name__)


class GoogleTrendsMonitor(TrendMonitor):
    source_name = "google_trends"

    def fetch(self) -> list[TrendTopic]:
        try:
            return self._fetch_pytrends()
        except Exception as exc:
            LOGGER.warning("Google pytrends request failed; trying RSS fallback: %s", exc)
            return self._fetch_rss_fallback()

    def _fetch_pytrends(self) -> list[TrendTopic]:
        try:
            from pytrends.request import TrendReq
        except ImportError as exc:
            raise MonitorError("pytrends is not installed") from exc

        country = self.config.get("country", "united_states")
        limit = int(self.config.get("limit", 25))
        hl = self.config.get("hl", "en-US")
        tz = int(self.config.get("tz", 360))

        pytrends = TrendReq(hl=hl, tz=tz, timeout=(10, 25), retries=2, backoff_factor=0.5)
        try:
            trending = pytrends.trending_searches(pn=country)
        except Exception as exc:
            LOGGER.warning("Google Trends request failed: %s", exc)
            raise MonitorError(str(exc)) from exc

        topics: list[TrendTopic] = []
        if trending is None:
            return topics

        values = trending.iloc[:, 0].dropna().astype(str).tolist()[:limit]
        for rank, title in enumerate(values, start=1):
            cleaned = clean_text(title)
            if len(cleaned) < 3:
                continue
            score = max(1.0, (limit - rank + 1) * 25.0)
            topics.append(
                TrendTopic(
                    source=self.source_name,
                    title=cleaned,
                    score=score,
                    volume=max(1, limit - rank + 1),
                    engagement=score,
                    raw={"rank": rank, "country": country},
                )
            )
        return topics

    def _fetch_rss_fallback(self) -> list[TrendTopic]:
        geo = str(self.config.get("rss_geo", "US"))
        limit = int(self.config.get("limit", 25))
        url = "https://trends.google.com/trending/rss"
        response = requests.get(
            url,
            params={"geo": geo},
            headers={"User-Agent": "ShortsMasterBot/1.0"},
            timeout=25,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        topics: list[TrendTopic] = []
        for rank, item in enumerate(root.findall(".//item")[:limit], start=1):
            title_node = item.find("title")
            if title_node is None or not title_node.text:
                continue
            title = clean_text(title_node.text)
            traffic_text = self._first_text_by_local_name(item, "approx_traffic") or ""
            traffic = self._traffic_to_int(traffic_text)
            item_link = self._child_text(item, "link") or ""
            published_at = self._child_text(item, "pubDate") or ""
            summary = clean_text(self._child_text(item, "description") or "")
            news_items = self._news_items(item)
            score = max(1.0, (limit - rank + 1) * 20.0 + min(300.0, traffic / 1_000.0))
            topics.append(
                TrendTopic(
                    source=self.source_name,
                    title=title,
                    url=item_link,
                    score=round(score, 2),
                    volume=traffic,
                    engagement=score,
                    raw={
                        "rank": rank,
                        "geo": geo,
                        "traffic": traffic_text,
                        "summary": summary,
                        "link": item_link,
                        "published_at": clean_text(published_at),
                        "news_items": news_items,
                        "source": "google_trends_rss",
                    },
                )
            )
        if not topics:
            raise MonitorError("Google Trends RSS returned no topics")
        return topics

    def _first_text_by_local_name(self, item: ET.Element, local_name: str) -> str | None:
        suffix = "}" + local_name
        for child in item:
            if child.tag == local_name or child.tag.endswith(suffix):
                return child.text
        return None

    def _child_text(self, item: ET.Element, local_name: str) -> str | None:
        found = item.find(local_name)
        if found is not None:
            return found.text
        return self._first_text_by_local_name(item, local_name)

    def _news_items(self, item: ET.Element) -> list[dict[str, str]]:
        news_items: list[dict[str, str]] = []
        for child in item:
            if not child.tag.endswith("news_item"):
                continue
            news: dict[str, str] = {}
            for nested in child:
                key = nested.tag.split("}")[-1]
                if nested.text:
                    news[key] = clean_text(nested.text)
            if news:
                news_items.append(news)
        return news_items

    def _traffic_to_int(self, value: str) -> int:
        cleaned = clean_text(value).lower().replace("+", "")
        match = re.search(r"([\d,.]+)\s*([km]?)", cleaned)
        if not match:
            return 0
        number = float(match.group(1).replace(",", ""))
        suffix = match.group(2)
        if suffix == "m":
            number *= 1_000_000
        elif suffix == "k":
            number *= 1_000
        return int(number)
