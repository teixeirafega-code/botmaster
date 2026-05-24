from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

from app.config.settings import GoogleTrendsSettings, TikTokSettings, TwitterSettings
from app.monitors.google_trends import GoogleTrendsMonitor
from app.monitors.tiktok_monitor import TikTokMonitor
from app.monitors.twitter_monitor import TwitterMonitor


def test_google_trends_does_not_pass_urllib3_retry_args(monkeypatch) -> None:
    captured_kwargs = {}

    class FakeTrendReq:
        def __init__(self, **kwargs) -> None:
            captured_kwargs.update(kwargs)

    pytrends_module = types.ModuleType("pytrends")
    request_module = types.ModuleType("pytrends.request")
    request_module.TrendReq = FakeTrendReq
    monkeypatch.setitem(sys.modules, "pytrends", pytrends_module)
    monkeypatch.setitem(sys.modules, "pytrends.request", request_module)
    monkeypatch.setattr(GoogleTrendsMonitor, "_fetch_trending_topics", lambda _self, _client: ["AI launch tool"])
    monkeypatch.setattr(GoogleTrendsMonitor, "_fetch_interest", lambda _self, _client, _topics: {})

    signals = GoogleTrendsMonitor(GoogleTrendsSettings()).collect()

    assert signals
    assert "retries" not in captured_kwargs
    assert "backoff_factor" not in captured_kwargs


def test_twitter_ntscraper_signal_parses_stats() -> None:
    monitor = TwitterMonitor(TwitterSettings(min_engagement=1, since_hours=48))

    signal = monitor._signal_from_ntscraper_tweet(
        {
            "text": "New #AITool launch is moving fast",
            "stats": {"likes": "1.2K", "retweets": "10", "comments": "5", "quotes": "2"},
            "date": datetime.now(timezone.utc).isoformat(),
            "link": "https://nitter.net/example/status/1",
        },
        "AI startup",
    )

    assert signal is not None
    assert signal.name == "#AITool"
    assert signal.social_engagement == 1227.0
    assert signal.metadata["source"] == "ntscraper"


def test_tiktok_hashtag_page_metadata_parser() -> None:
    monitor = TikTokMonitor(TikTokSettings())
    page = """
    <html>
      <head>
        <title>#aitools on TikTok</title>
        <meta name="description" content="Watch #aitools videos with 12.5M views and related #startup clips">
      </head>
      <body>{"viewCount":"12500000"}</body>
    </html>
    """

    metadata = monitor._extract_page_metadata(page, "aitools")

    assert metadata["view_count"] == 12_500_000
    assert "#aitools" in metadata["related_hashtags"]
    assert "#startup" in metadata["related_hashtags"]

