from __future__ import annotations

import logging

from app.publisher import YouTubePublisher
from app.services.database import ShortsMasterDatabase


LOGGER = logging.getLogger(__name__)


class MetricsTracker:
    def __init__(self, db: ShortsMasterDatabase, publisher: YouTubePublisher):
        self.db = db
        self.publisher = publisher

    def refresh(self, limit: int = 50) -> int:
        published = self.db.list_published(limit=limit)
        video_ids = [item["youtube_video_id"] for item in published if item.get("youtube_video_id")]
        stats = self.publisher.fetch_stats(video_ids)
        updated = 0
        for item in published:
            video_id = item.get("youtube_video_id")
            if not video_id or video_id not in stats:
                continue
            values = stats[video_id]
            self.db.record_metrics(
                queue_id=int(item["id"]),
                youtube_video_id=video_id,
                niche=item["niche"],
                views=values["views"],
                likes=values["likes"],
                comments=values["comments"],
            )
            updated += 1
        LOGGER.info("Metrics refresh updated %s videos", updated)
        return updated
