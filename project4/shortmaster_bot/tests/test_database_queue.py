from __future__ import annotations

from pathlib import Path

from app.models import QueueStatus, TrendTopic
from app.services.database import ShortsMasterDatabase


def test_seen_topics_prevent_repost(tmp_path: Path) -> None:
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="reddit", title="AI tools are changing video editing", score=100)

    assert not db.is_seen(topic.key)
    db.mark_seen(topic, selected=True)
    assert db.is_seen(topic.key)


def test_manual_queue_approval_flow(tmp_path: Path) -> None:
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="google_trends", title="New space discovery", score=80, niche="science")

    item = db.enqueue_topic(topic, QueueStatus.PENDING_APPROVAL)
    assert item["status"] == QueueStatus.PENDING_APPROVAL

    approved = db.approve(int(item["id"]))
    assert approved["status"] == QueueStatus.APPROVED
    assert approved["approved_at"] is not None


def test_background_category_is_stored_in_upload_history(tmp_path: Path) -> None:
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="reddit_story", title="Relato do Reddit com limpeza", score=90, niche="reddit_story")
    item = db.enqueue_topic(topic, QueueStatus.APPROVED)

    db.update_queue_item(int(item["id"]), background_category="deep_cleaning", quality_score=88.0)
    db.mark_published(int(item["id"]), "paper-test-id", paper_mode=True)
    updated = db.get_queue_item(int(item["id"]))
    performance = db.background_category_performance()

    assert updated is not None
    assert updated["background_category"] == "deep_cleaning"
    assert performance[0]["background_category"] == "deep_cleaning"
    assert performance[0]["video_count"] == 1


def test_background_filename_is_stored_in_upload_history(tmp_path: Path) -> None:
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="reddit_story", title="Relato com fundo local", score=90, niche="reddit_story")
    item = db.enqueue_topic(topic, QueueStatus.APPROVED)

    db.update_queue_item(
        int(item["id"]),
        background_category="custom_library",
        background_filename="cleaning-loop.mp4",
        background_source_url="https://example.com/cleaning-loop",
        background_license_type="royalty-free user license",
        background_commercial_rights_verified=1,
        quality_score=91.0,
    )
    updated = db.get_queue_item(int(item["id"]))

    assert updated is not None
    assert updated["background_filename"] == "cleaning-loop.mp4"
    assert updated["background_source_url"] == "https://example.com/cleaning-loop"
    assert updated["background_commercial_rights_verified"] == 1
    assert db.background_library_performance()[0]["filename"] == "cleaning-loop.mp4"
