from __future__ import annotations

import json
from pathlib import Path

from app.models import ContentScript, QueueStatus, ResearchBrief, TrendTopic
from app.services.pipeline import ShortsMasterPipeline
from app.services.youtube_publish_scheduler import YouTubePublishScheduler
from tests.conftest import make_test_config


def test_upload_slots_spread_five_times_per_day(tmp_path: Path) -> None:
    pipeline = ShortsMasterPipeline(make_test_config(tmp_path))
    service = YouTubePublishScheduler(pipeline, pipeline.config)

    assert service.upload_slots() == ["08:00", "11:30", "15:00", "18:30", "22:00"]


def test_scheduler_skips_when_live_upload_disabled(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    item = ready_item(pipeline, tmp_path, "Ciência permanente", title="Ciência permanente")
    service = YouTubePublishScheduler(pipeline, config)

    result = service.publish_next_ready()

    assert result["status"] == "skipped"
    assert result["queue_id"] == item["id"]
    assert "real upload disabled" in result["reason"]
    assert pipeline.db.get_queue_item(int(item["id"]))["youtube_video_id"] is None


def test_scheduler_blocks_low_quality_before_upload(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    item = ready_item(pipeline, tmp_path, "Vídeo de baixa qualidade", title="Vídeo de baixa qualidade", quality_score=60.0)
    pipeline.publisher.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not upload"))

    result = YouTubePublishScheduler(pipeline, config).publish_next_ready()

    assert result["status"] == "validation_failed"
    assert "quality_score_minimum" in result["reason"]
    assert pipeline.db.get_queue_item(int(item["id"]))["youtube_video_id"] is None


def test_scheduler_blocks_duplicate_title_before_upload(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    first = ready_item(pipeline, tmp_path, "Título compartilhado", title="Mesmo título de envio")
    pipeline.db.mark_published(int(first["id"]), "already-live", paper_mode=False)
    second = ready_item(pipeline, tmp_path, "Tema diferente", title="Mesmo título de envio")
    pipeline.publisher.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not upload"))

    result = YouTubePublishScheduler(pipeline, config).publish_next_ready()

    assert result["queue_id"] == second["id"]
    assert result["status"] == "validation_failed"
    assert "duplicate_title_absent" in result["reason"]


def test_scheduler_blocks_background_without_verified_commercial_rights(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    item = ready_item(pipeline, tmp_path, "Relato com fundo sem licenca")
    pipeline.db.update_queue_item(
        int(item["id"]),
        background_filename="unverified.mp4",
        background_license_type="commercial rights not verified",
        background_commercial_rights_verified=0,
    )
    pipeline.publisher.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("must not upload")
    )

    result = YouTubePublishScheduler(pipeline, config).publish_next_ready()

    assert result["status"] == "validation_failed"
    assert "background_commercial_rights_verified" in result["reason"]


def test_scheduler_uploads_oldest_ready_video_first_with_fake_publisher(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    first = ready_item(pipeline, tmp_path, "Primeiro tema", title="Primeiro título de envio")
    second = ready_item(pipeline, tmp_path, "Segundo tema", title="Segundo título de envio")
    uploaded: list[int] = []

    def fake_publish(_video_path, _script, queue_item):
        uploaded.append(int(queue_item["id"]))
        return f"real-video-{queue_item['id']}"

    pipeline.publisher.publish = fake_publish

    result = YouTubePublishScheduler(pipeline, config).publish_next_ready()

    assert result["status"] == "success"
    assert result["queue_id"] == first["id"]
    assert uploaded == [first["id"]]
    assert pipeline.db.get_queue_item(int(first["id"]))["status"] == QueueStatus.PUBLISHED
    assert pipeline.db.get_queue_item(int(second["id"]))["status"] == QueueStatus.READY


def test_scheduler_resolves_stale_absolute_video_path_by_filename(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    item = ready_item(pipeline, tmp_path, "Relato com caminho antigo", title="Envio com caminho antigo")
    original_name = Path(str(item["video_path"])).name
    restored_path = pipeline.video.output_dir / original_name
    restored_path.write_bytes(b"restored rendered video")
    stale_path = Path("/home/runner/work/botmaster/botmaster/project4/shortmaster_bot/videos/rendered") / original_name
    pipeline.db.update_queue_item(int(item["id"]), video_path=str(stale_path))
    uploaded_paths: list[Path] = []

    def fake_publish(video_path, _script, queue_item):
        uploaded_paths.append(Path(video_path))
        return f"real-video-{queue_item['id']}"

    pipeline.publisher.publish = fake_publish

    result = YouTubePublishScheduler(pipeline, config).publish_next_ready()

    assert result["status"] == "success"
    assert result["uploaded"] is True
    assert uploaded_paths == [restored_path]
    assert pipeline.db.get_queue_item(int(item["id"]))["video_path"] == str(restored_path)


def test_scheduler_rerenders_ready_item_when_cached_video_is_missing(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    item = ready_item(pipeline, tmp_path, "Relato sem MP4 restaurado", title="Envio refeito")
    missing_path = Path(str(item["video_path"]))
    missing_path.unlink()
    calls = {"process": 0}
    uploaded_paths: list[Path] = []

    def fake_process(process_item, publish_after_generate=False):
        calls["process"] += 1
        assert publish_after_generate is False
        rerendered = pipeline.video.output_dir / "rerendered-ready-video.mp4"
        rerendered.write_bytes(b"rerendered video")
        pipeline.db.update_queue_item(
            int(process_item["id"]),
            status=QueueStatus.READY,
            video_path=str(rerendered),
            approved_for_live_upload=1,
        )
        return pipeline.db.get_queue_item(int(process_item["id"])) or process_item

    def fake_publish(video_path, _script, queue_item):
        uploaded_paths.append(Path(video_path))
        return f"real-video-{queue_item['id']}"

    pipeline.process_item = fake_process
    pipeline.publisher.publish = fake_publish

    result = YouTubePublishScheduler(pipeline, config).publish_next_ready()

    assert result["status"] == "success"
    assert result["uploaded"] is True
    assert calls["process"] == 1
    assert uploaded_paths == [pipeline.video.output_dir / "rerendered-ready-video.mp4"]


def test_auto_approval_requires_quality_safety_and_verified_background_rights(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    config["queue"]["auto_approve_live_upload"] = True
    pipeline = ShortsMasterPipeline(config)
    topic = TrendTopic(source="reddit_story", title="Relato pronto", score=100, niche="reddit_story")
    item = pipeline.db.enqueue_topic(topic, QueueStatus.READY)
    pipeline.db.update_queue_item(
        int(item["id"]),
        background_filename="verified.mp4",
        background_license_type="original commercial license",
        background_commercial_rights_verified=1,
    )

    approved = pipeline._apply_auto_live_approval(
        int(item["id"]),
        {"quality_score": 82.0, "safety_score": 94.0},
    )

    updated = pipeline.db.get_queue_item(int(item["id"]))
    assert approved is True
    assert updated is not None
    assert updated["approved_for_live_upload"] == 1


def test_auto_approval_blocks_unverified_background_rights(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    config["queue"]["auto_approve_live_upload"] = True
    pipeline = ShortsMasterPipeline(config)
    topic = TrendTopic(source="reddit_story", title="Relato bloqueado", score=100, niche="reddit_story")
    item = pipeline.db.enqueue_topic(topic, QueueStatus.READY)
    pipeline.db.update_queue_item(
        int(item["id"]),
        background_filename="unverified.mp4",
        background_commercial_rights_verified=0,
    )

    approved = pipeline._apply_auto_live_approval(
        int(item["id"]),
        {"quality_score": 90.0, "safety_score": 98.0},
    )

    updated = pipeline.db.get_queue_item(int(item["id"]))
    assert approved is False
    assert updated is not None
    assert updated["approved_for_live_upload"] == 0
    assert "commercial rights are not verified" in updated["upload_blocked_reason"]


def test_scheduler_pauses_after_three_upload_failures(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    ready_item(pipeline, tmp_path, "Envio com falha", title="Título de envio com falha")

    def fake_failure(*_args, **_kwargs):
        raise RuntimeError("simulated upload failure")

    pipeline.publisher.publish = fake_failure
    service = YouTubePublishScheduler(pipeline, config)

    for _ in range(3):
        result = service.publish_next_ready()

    assert result["status"] == "failure"
    assert service.is_paused()
    assert "consecutive upload failures" in service.pause_reason()


def test_scheduler_auto_resumes_upload_failure_pause_and_retries_ready_candidate(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    item = ready_item(
        pipeline,
        tmp_path,
        "Historia de reenvio",
        title="A chave que apareceu de novo na mesa",
    )
    queue_id = int(item["id"])
    pipeline.db.update_queue_item(
        queue_id,
        upload_attempt_count=3,
        last_upload_error="old OAuth failure",
        upload_blocked_reason="old OAuth failure",
        error="old OAuth failure",
    )
    service = YouTubePublishScheduler(pipeline, config)
    service.pause("stopped after 3 consecutive upload failures")
    uploaded: list[int] = []

    def fake_publish(_video_path, _script, queue_item):
        uploaded.append(int(queue_item["id"]))
        return "retried-private-video"

    pipeline.publisher.publish = fake_publish

    result = service.publish_next_ready()

    assert result["status"] == "success"
    assert result["uploaded"] is True
    assert result["queue_id"] == queue_id
    assert result["auto_resume"]["resumed"] is True
    assert result["auto_resume"]["upload_retry_reset"]["queue_ids"] == [queue_id]
    assert uploaded == [queue_id]
    updated = pipeline.db.get_queue_item(queue_id)
    assert updated is not None
    assert updated["upload_attempt_count"] == 1
    assert updated["youtube_video_id"] == "retried-private-video"
    assert service.is_paused() is False


def test_scheduler_does_not_auto_resume_validation_pause(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    ready_item(pipeline, tmp_path, "Paused validation story", title="Paused validation upload")
    service = YouTubePublishScheduler(pipeline, config)
    service.pause("stopped because validation failure rate is too high: 5/6")
    pipeline.publisher.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("must not upload while paused for validation")
    )

    result = service.publish_next_ready()

    assert result["status"] == "paused"
    assert result["auto_resume"]["resumed"] is False
    assert "not an upload-failure retry pause" in result["auto_resume"]["reason"]
    assert service.is_paused() is True


def test_scheduler_resume_resets_consecutive_upload_failure_count(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    pipeline = ShortsMasterPipeline(config)
    service = YouTubePublishScheduler(pipeline, config)
    pipeline.db.record_scheduler_event("upload", "failure", reason="first")
    pipeline.db.record_scheduler_event("upload", "failure", reason="second")

    assert pipeline.db.consecutive_upload_failures() == 2

    result = service.resume(reason="credentials fixed")

    assert result["status"] == "resumed"
    assert pipeline.db.consecutive_upload_failures() == 0
    assert service.is_paused() is False


def live_config(tmp_path: Path) -> dict:
    config = make_test_config(tmp_path)
    config["root_dir"] = str(tmp_path)
    config["app"]["paper_mode"] = False
    config["publishing"]["enable_real_upload"] = True
    config["publishing"]["live_upload_enabled"] = True
    config["publishing"]["channel_id"] = "UC-test"
    secret = tmp_path / "secrets" / "client.json"
    token = tmp_path / "secrets" / "token.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("{}", encoding="utf-8")
    token.write_text("{}", encoding="utf-8")
    return config


def ready_item(
    pipeline: ShortsMasterPipeline,
    tmp_path: Path,
    topic_title: str,
    title: str = "Ready Upload Title",
    quality_score: float = 85.0,
) -> dict:
    topic = TrendTopic(source="curated", title=topic_title, score=100, niche="science")
    item = pipeline.db.enqueue_topic(topic, QueueStatus.READY)
    video_path = tmp_path / f"{topic.key}.mp4"
    video_path.write_bytes(b"ready video")
    script = script_payload(title)
    research = research_payload(topic_title)
    pipeline.db.update_queue_item(
        int(item["id"]),
        status=QueueStatus.READY,
        script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
        research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
        video_path=str(video_path),
        quality_score=quality_score,
        safety_json=json.dumps({"safety_score": 96.0, "quality_score": quality_score}, ensure_ascii=True),
        approved_for_live_upload=1,
    )
    return pipeline.db.get_queue_item(int(item["id"])) or item


def script_payload(title: str) -> ContentScript:
    narration = (
        "Este vídeo curto tem uma abertura específica, uma seção clara de contexto e três fatos concretos. "
        "Primeiro, o tema tem uma fonte datada. Segundo, a fonte explica o detalhe técnico. "
        "Terceiro, o final entrega uma conclusão simples para o público lembrar."
    )
    return ContentScript(
        title=title,
        hook="Um detalhe específico muda a história.",
        narration=narration,
        scenes=[
            {"caption": "Detalhe específico", "image_prompt": "imagem vertical de tema científico"},
            {"caption": "Contexto", "image_prompt": "imagem vertical de pesquisa"},
            {"caption": "Fato um", "image_prompt": "imagem vertical factual"},
            {"caption": "Fato dois", "image_prompt": "imagem vertical técnica"},
        ],
        tags=["ciencia", "shortsbr"],
        description="Vídeo curto de ciência em português com pesquisa de conteúdo permanente.",
        niche="science",
    )


def research_payload(topic: str) -> ResearchBrief:
    return ResearchBrief(
        topic=topic,
        category="science",
        why_now="Evergreen source context exists.",
        concrete_facts=[
            "The source gives a dated mission fact.",
            "The source gives a technical detail.",
            "The source gives a location detail.",
        ],
        source_urls=["https://science.nasa.gov/"],
        source_summaries=[
            {
                "source": "NASA official site",
                "title": topic,
                "summary": "Three concrete facts.",
                "url": "https://science.nasa.gov/",
                "source_date": "2025-01-01T00:00:00+00:00",
                "source_trust_score": 88.0,
            }
        ],
        freshness_score=100.0,
        freshness_threshold=70.0,
        requires_fresh_source=False,
        trust_score=88.0,
        trust_threshold=70.0,
        requires_trusted_source=False,
    )
