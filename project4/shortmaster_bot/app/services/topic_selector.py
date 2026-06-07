from __future__ import annotations

import logging
from collections import OrderedDict

from app.models import TrendTopic
from app.services.database import ShortsMasterDatabase


LOGGER = logging.getLogger(__name__)


class TopicSelector:
    def __init__(self, db: ShortsMasterDatabase, config: dict):
        self.db = db
        self.config = config
        self.selection_config = config.get("selection", {})

    def select_best(self, topics: list[TrendTopic]) -> TrendTopic | None:
        if not topics:
            return None
        self.db.record_observations(topics)

        unique: OrderedDict[str, TrendTopic] = OrderedDict()
        for topic in topics:
            topic.niche = self.classify_niche(topic.title, topic.hashtags)
            adjusted_score = self.adjusted_score(topic)
            topic.score = round(adjusted_score, 2)
            existing = unique.get(topic.key)
            if existing is None or topic.score > existing.score:
                unique[topic.key] = topic

        candidates = [topic for topic in unique.values() if not self.db.is_seen(topic.key)]
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            LOGGER.info("No unseen trend candidates found")
            return None
        best = candidates[0]
        LOGGER.info(
            "Selected trend '%s' from %s with score %.2f in niche %s",
            best.title,
            best.source,
            best.score,
            best.niche,
        )
        return best

    def adjusted_score(self, topic: TrendTopic) -> float:
        source_weights = self.selection_config.get("source_weights", {})
        source_weight = float(source_weights.get(topic.source, 1.0))
        niche_multiplier = self.db.get_niche_multiplier(topic.niche)
        hashtag_boost = 1.08 if topic.hashtags else 1.0
        return max(0.0, topic.score * source_weight * niche_multiplier * hashtag_boost)

    def classify_niche(self, title: str, hashtags: list[str] | None = None) -> str:
        text = " ".join([title, *(hashtags or [])]).lower()
        niches = self.selection_config.get("niches", {})
        for niche, keywords in niches.items():
            if any(str(keyword).lower() in text for keyword in keywords):
                return str(niche)
        return str(self.selection_config.get("default_niche", "general"))
