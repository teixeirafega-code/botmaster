from __future__ import annotations

from pathlib import Path

from app.services.pipeline import ShortsMasterPipeline
from app.services.scheduled_job import ScheduledPublishingJob
from app.services.youtube_publish_scheduler import YouTubePublishScheduler
from tests.test_youtube_publish_scheduler import live_config, ready_item


def test_scheduled_job_uploads_at_most_one_video_and_exits(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    first = ready_item(pipeline, tmp_path, "Primeiro job", title="Primeiro envio privado")
    second = ready_item(pipeline, tmp_path, "Segundo job", title="Segundo envio privado")
    uploaded: list[int] = []

    def fake_publish(_video_path, _script, queue_item):
        uploaded.append(int(queue_item["id"]))
        return f"scheduled-video-{queue_item['id']}"

    pipeline.publisher.publish = fake_publish
    pipeline.refresh_metrics = lambda: 0

    result = ScheduledPublishingJob(pipeline, config).run(job_id="test-run")

    assert result["status"] == "success"
    assert result["generated_count"] == 0
    assert result["upload_attempt_count"] == 1
    assert result["uploaded_count"] == 1
    assert uploaded == [int(first["id"])]
    assert pipeline.db.get_queue_item(int(second["id"]))["youtube_video_id"] is None
    assert (tmp_path / "reports" / "scheduled_job_report.json").exists()
    assert (tmp_path / "reports" / "scheduled_job_test-run.json").exists()


def test_scheduled_job_generates_at_most_one_queue_item(tmp_path: Path, monkeypatch) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    calls = {"discover": 0, "process": 0, "publish": 0}

    def fake_discover():
        calls["discover"] += 1
        return {"id": 1, "status": "approved", "title": "Tema"}

    def fake_process(item, publish_after_generate=False):
        calls["process"] += 1
        assert publish_after_generate is False
        return {**item, "status": "safety_blocked", "approved_for_live_upload": 0}

    def fake_publish_next_ready(_self):
        calls["publish"] += 1
        return {"status": "empty", "uploaded": False}

    monkeypatch.setattr(pipeline, "discover_and_queue", fake_discover)
    monkeypatch.setattr(pipeline, "process_item", fake_process)
    monkeypatch.setattr(
        YouTubePublishScheduler,
        "publish_next_ready",
        fake_publish_next_ready,
    )
    pipeline.refresh_metrics = lambda: 0

    result = ScheduledPublishingJob(pipeline, config).run(job_id="bounded-run")

    assert result["generated_count"] == 1
    assert result["uploaded_count"] == 0
    assert calls == {"discover": 1, "process": 1, "publish": 1}
