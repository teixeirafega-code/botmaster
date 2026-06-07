from __future__ import annotations

from pathlib import Path

from app.models import ContentScript, QueueStatus, TrendTopic
from app.publisher.youtube import YouTubePublisher
from app.services.database import ShortsMasterDatabase
from app.services.safety import SafetyGuard
from tests.conftest import make_test_config


def make_script(title: str = "Ferramentas de IA: a pista escondida") -> ContentScript:
    narration = (
        "Ferramentas de IA têm uma pista escondida. O estranho não é o barulho. "
        "Primeira pista: criadores testam ferramentas dentro de fluxos reais de edição. "
        "Depois fica mais claro: equipes comparam velocidade, custo e qualidade do resultado. "
        "O detalhe que muita gente perde: uma ferramenta útil remove uma etapa chata, não todo o trabalho. "
        "Isso aumenta a tensão: atalhos ruins gastam mais tempo do que economizam. "
        "Uma edição pode parecer rápida, mas o teste real é repetir amanhã sem consertar bagunça. "
        "Se a mesma ferramenta funcionar de novo com consistência, ela ganha confiança. "
        "Aqui vem a revelação: vence a ferramenta que desaparece no fluxo. "
        "Final rápido: lembre da pista, tempo salvo vence demonstração barulhenta."
    )
    scenes = [
        {"caption": "Pista da IA", "image_prompt": "visual vertical de ferramentas de IA em mesa de edição, sem texto"},
        {"caption": "Não é barulho", "image_prompt": "visual vertical de ferramentas de IA com telas discretas, sem logos"},
        {"caption": "Fluxo real", "image_prompt": "visual vertical de fluxo de edição com assistente de IA, sem texto"},
        {"caption": "Velocidade testada", "image_prompt": "visual vertical de cronômetro e linha de edição, ferramentas de IA"},
        {"caption": "Custo comparado", "image_prompt": "visual vertical de orçamento de criador com ferramentas de IA, sem texto legível"},
        {"caption": "Qualidade decide", "image_prompt": "visual vertical de revisão de resultado com ferramentas de IA, sem texto"},
        {"caption": "Etapa removida", "image_prompt": "visual vertical de automação removendo tarefa repetitiva, ferramentas de IA"},
        {"caption": "Atalhos ruins", "image_prompt": "visual vertical de fluxo quebrado com ferramentas de IA"},
        {"caption": "A revelação", "image_prompt": "visual vertical de ferramentas de IA sumindo em fluxo limpo, sem logos"},
        {"caption": "Tempo salvo", "image_prompt": "visual vertical de criador finalizando edição mais rápido com ferramentas de IA"},
    ]
    return ContentScript(
        title=title,
        hook="Ferramentas de IA têm uma pista escondida.",
        narration=narration,
        scenes=scenes,
        tags=["ia", "tecnologia", "shortsbr"],
        description="Explicação original em português sobre uma tendência de IA.",
        niche="technology",
    )


def test_safety_blocks_spammy_script(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    guard = SafetyGuard(db, config)
    script = make_script("MAKE MONEY FAST!!! 100% GUARANTEED!!!")
    topic = TrendTopic(source="reddit", title="Tendência de dinheiro com IA", score=100, niche="technology")

    decision = guard.evaluate_content(1, topic, script)

    assert decision["allowed"] is False
    assert decision["quality_score"] < 70
    assert any("spam" in reason or "exclamation" in reason for reason in decision["reasons"])


def test_safety_blocks_duplicate_local_script(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="reddit", title="Ferramentas de IA em alta", score=100, niche="technology")
    first = db.enqueue_topic(topic, QueueStatus.APPROVED)
    script = make_script()
    db.update_queue_item(int(first["id"]), script_json=__import__("json").dumps(script.to_dict()))

    second_topic = TrendTopic(source="google_trends", title="Ferramentas de IA para criadores", score=120, niche="technology")
    second = db.enqueue_topic(second_topic, QueueStatus.APPROVED)
    decision = SafetyGuard(db, config).evaluate_content(int(second["id"]), second_topic, script)

    assert decision["allowed"] is False
    assert any("too similar" in reason for reason in decision["reasons"])


def test_safety_does_not_penalize_current_selected_topic(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="google_trends", title="Ferramentas de IA em alta", score=100, niche="technology")
    item = db.enqueue_topic(topic, QueueStatus.APPROVED)
    db.mark_seen(topic, selected=True)

    decision = SafetyGuard(db, config).evaluate_content(int(item["id"]), topic, make_script())

    assert decision["allowed"] is True
    assert "topic key is already recorded in history" not in decision["warnings"]


def test_safety_blocks_weak_hook(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="google_trends", title="Ferramentas de IA em alta", score=100, niche="technology")
    script = make_script()
    script.hook = "Ferramentas de IA estão por todo lado e todo mundo está falando delas."

    decision = SafetyGuard(db, config).evaluate_content(1, topic, script)

    assert decision["allowed"] is False
    assert any("hook_score" in reason for reason in decision["reasons"])


def test_safety_blocks_static_slide_visuals(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="google_trends", title="Ferramentas de IA em alta", score=100, niche="technology")
    script = make_script()
    script.scenes = [
        {
            "caption": "Esta legenda longa parece relatório e lota a tela",
            "image_prompt": "slide de gradiente estático com texto",
        }
        for _ in range(5)
    ]

    decision = SafetyGuard(db, config).evaluate_content(1, topic, script)

    assert decision["allowed"] is False
    assert any("visual_interest_score" in reason for reason in decision["reasons"])


def test_upload_limit_blocks_real_upload_after_daily_cap(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    config["publishing"]["enable_real_upload"] = True
    config["publishing"]["live_upload_enabled"] = True
    config["publishing"]["daily_upload_limit"] = 1
    config["app"]["paper_mode"] = False
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="reddit", title="One upload today", score=100)
    item = db.enqueue_topic(topic, QueueStatus.APPROVED)
    db.mark_published(int(item["id"]), "real-video-id", paper_mode=False)

    decision = SafetyGuard(db, config).evaluate_upload(real_upload_enabled=True)

    assert decision["allowed"] is False
    assert any("daily_upload_limit_available" in reason for reason in decision["reasons"])


def test_live_upload_requires_all_three_flags(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    config["app"]["paper_mode"] = False
    config["publishing"]["enable_real_upload"] = True
    config["publishing"]["live_upload_enabled"] = False

    publisher = YouTubePublisher(config)

    assert publisher.real_upload_enabled is False


def test_pre_upload_blocks_missing_live_approval_and_credentials(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    config["root_dir"] = str(tmp_path)
    config["app"]["paper_mode"] = False
    config["publishing"]["enable_real_upload"] = True
    config["publishing"]["live_upload_enabled"] = True
    config["publishing"]["channel_id"] = "UC-test"
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(source="google_trends", title="Ferramentas de IA em alta", score=100)
    item = db.enqueue_topic(topic, QueueStatus.READY)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not uploaded")

    decision = SafetyGuard(db, config).evaluate_upload(
        real_upload_enabled=True,
        queue_id=int(item["id"]),
        topic=topic,
        script=make_script(),
        video_path=video,
        queue_item=item,
    )

    assert decision["allowed"] is False
    assert any("approved_for_live_upload" in reason for reason in decision["reasons"])
    assert any("client_secrets_present" in reason for reason in decision["reasons"])
    assert (tmp_path / "reports" / "youtube_pre_upload_checklist.json").exists()


def test_pre_upload_quota_blocks_when_units_exhausted(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    config["root_dir"] = str(tmp_path)
    config["app"]["paper_mode"] = False
    config["publishing"]["enable_real_upload"] = True
    config["publishing"]["live_upload_enabled"] = True
    config["publishing"]["channel_id"] = "UC-test"
    config["publishing"]["youtube_daily_quota_units"] = 1600
    config["publishing"]["youtube_upload_quota_units"] = 1600
    secrets = tmp_path / "secrets" / "client.json"
    token = tmp_path / "secrets" / "token.json"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("{}", encoding="utf-8")
    token.write_text("{}", encoding="utf-8")
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    db.record_youtube_quota_usage(1, "videos.insert", 1600)
    topic = TrendTopic(source="google_trends", title="Ferramentas de IA em alta", score=100)
    item = db.enqueue_topic(topic, QueueStatus.READY)
    item = db.approve_for_live_upload(int(item["id"]))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not uploaded")

    decision = SafetyGuard(db, config).evaluate_upload(
        real_upload_enabled=True,
        queue_id=int(item["id"]),
        topic=topic,
        script=make_script(),
        video_path=video,
        queue_item=item,
    )

    assert decision["allowed"] is False
    assert any("youtube_quota_available" in reason for reason in decision["reasons"])


def test_publisher_simulates_when_real_upload_disabled(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    config["app"]["paper_mode"] = False
    config["publishing"]["enable_real_upload"] = False
    publisher = YouTubePublisher(config)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not a real video but not uploaded")

    video_id = publisher.publish(video, make_script(), {"id": 7})

    assert video_id.startswith("paper-7-")
    assert publisher.real_upload_enabled is False
