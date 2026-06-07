from __future__ import annotations

from pathlib import Path

from app.models import TrendTopic
from app.services.database import ShortsMasterDatabase
from app.services.topic_selector import TopicSelector
from tests.conftest import make_test_config


def test_selector_scores_niche_and_skips_seen(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    selector = TopicSelector(db, config)

    seen = TrendTopic(source="reddit", title="AI robot goes viral", score=500)
    db.mark_seen(seen, selected=True)
    candidates = [
        seen,
        TrendTopic(source="google_trends", title="Bitcoin price question", score=100),
        TrendTopic(source="reddit", title="Small general curiosity", score=110),
    ]

    best = selector.select_best(candidates)

    assert best is not None
    assert best.title == "Bitcoin price question"
    assert best.niche == "finance"


def test_classifier_defaults_to_general(tmp_path: Path) -> None:
    selector = TopicSelector(ShortsMasterDatabase(tmp_path / "bot.db"), make_test_config(tmp_path))

    assert selector.classify_niche("A strange question everyone is asking") == "general"
