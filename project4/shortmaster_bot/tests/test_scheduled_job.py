from __future__ import annotations

import json
from pathlib import Path

from app.services.pipeline import ShortsMasterPipeline
from app.services.scheduled_job import ScheduledPublishingJob
from app.services.youtube_publish_scheduler import YouTubePublishScheduler
from app.models import ContentScript, QueueStatus, TrendTopic
from tests.test_youtube_publish_scheduler import live_config, ready_item, research_payload


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


def test_scheduled_job_retries_generation_until_upload_ready_limit(tmp_path: Path, monkeypatch) -> None:
    config = live_config(tmp_path)
    config.setdefault("scheduler", {})["generation_attempt_limit"] = 3
    pipeline = ShortsMasterPipeline(config)
    calls = {"discover": 0, "process": 0, "publish": 0}

    def fake_discover(**_kwargs):
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
    assert result["generation_result"]["attempt_count"] == 3
    assert result["workflow_stages"]["story_selected"]["passed"] is True
    assert result["workflow_stages"]["queued_ready"]["passed"] is False
    assert result["workflow_stages"]["upload_attempted"]["passed"] is False
    for attempt in result["generation_result"]["attempts"]:
        assert attempt["failure_stage"]
        assert attempt["failure_reason"]
        assert "attempt_gates" in attempt
    assert result["upload_result"]["status"] == "generation_failed"
    assert calls == {"discover": 3, "process": 3, "publish": 0}


def test_scheduled_job_reports_pt_br_failure_per_attempt(tmp_path: Path, monkeypatch) -> None:
    config = live_config(tmp_path)
    config.setdefault("scheduler", {})["generation_attempt_limit"] = 1
    pipeline = ShortsMasterPipeline(config)

    def fake_discover(**_kwargs):
        return {"id": 1, "status": "approved", "title": "Tema"}

    def fake_process(item, publish_after_generate=False):
        assert publish_after_generate is False
        return {
            **item,
            "status": QueueStatus.SAFETY_BLOCKED,
            "approved_for_live_upload": 0,
            "upload_blocked_reason": "English text remains in final output: story",
            "safety_json": json.dumps(
                {
                    "quality_score": 0.0,
                    "safety_score": 0.0,
                    "checks": {
                        "required_language": "pt-BR",
                        "script_language": "pt-BR",
                        "narration_language": "pt-BR",
                        "subtitle_language": "pt-BR",
                        "title_language": "pt-BR",
                        "description_language": "pt-BR",
                        "hashtag_language": "pt-BR",
                        "english_residue": [{"field": "hashtags", "term": "story"}],
                    },
                    "reasons": ["English text remains in final output: story"],
                },
                ensure_ascii=True,
            ),
        }

    monkeypatch.setattr(pipeline, "discover_and_queue", fake_discover)
    monkeypatch.setattr(pipeline, "process_item", fake_process)
    pipeline.refresh_metrics = lambda: 0

    result = ScheduledPublishingJob(pipeline, config).run(job_id="pt-br-failure")
    attempt = result["generation_result"]["attempts"][0]

    assert result["status"] == "generation_failed"
    assert attempt["failure_stage"] == "pt-BR"
    assert attempt["failure_reason"] == "English text remains in final output: story"
    assert attempt["attempt_gates"]["pt-BR"]["passed"] is False
    assert result["workflow_stages"]["upload_attempted"]["passed"] is False


def test_real_upload_job_generates_ready_item_then_uploads_private_video(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    config["queue"]["auto_approve_live_upload"] = True
    config.setdefault("scheduler", {})["generation_attempt_limit"] = 3
    pipeline = ShortsMasterPipeline(config)
    uploaded: list[int] = []

    def fake_discover(**_kwargs):
        topic = TrendTopic(
            source="reddit_story",
            title="Reddit-style original story seed: A chave errada",
            score=120_000,
            niche="reddit_story",
            raw={"content_mode": "reddit_story"},
        )
        return pipeline.db.enqueue_topic(topic, QueueStatus.APPROVED)

    def fake_process(item, publish_after_generate=False):
        assert publish_after_generate is False
        queue_id = int(item["id"])
        video_path = tmp_path / f"real-upload-{queue_id}.mp4"
        video_path.write_bytes(b"ready private upload video")
        prompt = "Pra você, qual detalhe foi mais estranho?"
        script = ContentScript(
            title="A chave que abriu o apartamento errado",
            hook="Pessoal do Reddit: qual erro de chave fez você gelar?",
            narration=(
                "Pessoal do Reddit: qual erro de chave fez você gelar? "
                "Eu cheguei tarde no prédio e peguei a chave reserva com o porteiro. "
                "A porta abriu fácil demais. Só que aquele não era meu apartamento. "
                "Na parede, tinha uma foto do meu próprio corredor. Eu tentei sair sem fazer barulho. "
                "Então ouvi passos subindo a escada. A pessoa parou exatamente na porta. "
                "Meu celular vibrou com uma mensagem sem número. Ela dizia: agora você sabe qual chave funciona. "
                "Eu larguei tudo, desci correndo e dormi na casa de um amigo. "
                "No dia seguinte, o porteiro jurou que nunca tinha me entregado chave nenhuma. "
                f"{prompt}"
            ),
            scenes=[
                {"caption": "Chave reserva", "image_prompt": "vídeo satisfatório aprovado acompanhando chave errada"},
                {"caption": "Porta aberta", "image_prompt": "vídeo satisfatório aprovado acompanhando porta abrindo"},
                {"caption": "Apartamento errado", "image_prompt": "vídeo satisfatório aprovado acompanhando corredor estranho"},
                {"caption": "Foto do corredor", "image_prompt": "vídeo satisfatório aprovado acompanhando fotografia na parede"},
                {"caption": "Passos na escada", "image_prompt": "vídeo satisfatório aprovado acompanhando suspense"},
                {"caption": "Mensagem sem número", "image_prompt": "vídeo satisfatório aprovado acompanhando celular vibrando"},
                {"caption": "Saí correndo", "image_prompt": "vídeo satisfatório aprovado acompanhando fuga"},
                {"caption": "Porteiro negou", "image_prompt": "vídeo satisfatório aprovado acompanhando portaria vazia"},
                {"caption": "A chave sumiu", "image_prompt": "vídeo satisfatório aprovado acompanhando chave desaparecida"},
                {"caption": "Detalhe estranho", "image_prompt": "vídeo satisfatório aprovado acompanhando mistério final"},
            ],
            tags=["historiasreddit", "relatos", "shortsbr", "misterio"],
            description="Uma chave entregue tarde da noite abre uma porta que não deveria abrir.",
            niche="reddit_story",
        )
        script.engagement_prompt = prompt
        script.engagement_prompt_type = "personal_experience"
        script.engagement_score = 91.0
        script.engagement_prompt_variants = [
            {"text": prompt, "score": 91.0, "type": "personal_experience"},
            {"text": "Você teria entrado ou saído correndo?", "score": 88.0, "type": "opinion"},
            {"text": "Qual parte te deixou mais desconfiado?", "score": 87.0, "type": "curiosity"},
        ]
        research = research_payload("A chave errada")
        pipeline.db.update_queue_item(
            queue_id,
            status=QueueStatus.READY,
            script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
            research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
            video_path=str(video_path),
            quality_score=88.0,
            safety_json=json.dumps({"quality_score": 88.0, "safety_score": 96.0}, ensure_ascii=True),
            background_filename="shortmaster-original-pressure-washing-v1.mp4",
            background_license_type="original procedural work generated by ShortMaster; commercial use verified",
            background_commercial_rights_verified=1,
            approved_for_live_upload=1,
        )
        return pipeline.db.get_queue_item(queue_id) or item

    def fake_publish(_video_path, _script, queue_item):
        uploaded.append(int(queue_item["id"]))
        return "manual-real-upload-private-video"

    pipeline.discover_and_queue = fake_discover
    pipeline.process_item = fake_process
    pipeline.publisher.publish = fake_publish
    pipeline.refresh_metrics = lambda: 0

    result = ScheduledPublishingJob(pipeline, config).run(job_id="real-upload-run")

    assert result["status"] == "success"
    assert result["generated_count"] == 1
    assert result["uploaded_count"] == 1
    assert result["generation_result"]["upload_ready"] is True
    assert result["workflow_stages"]["story_selected"]["passed"] is True
    assert result["workflow_stages"]["video_rendered"]["passed"] is True
    assert result["workflow_stages"]["validation_passed"]["passed"] is True
    assert result["workflow_stages"]["queued_ready"]["passed"] is True
    assert result["workflow_stages"]["upload_attempted"]["passed"] is True
    assert result["upload_result"]["youtube_video_id"] == "manual-real-upload-private-video"
    assert uploaded == [result["generation_result"]["queue_id"]]
