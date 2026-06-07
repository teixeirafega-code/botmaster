from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import ContentScript, QueueStatus, ResearchBrief, TrendTopic
from app.services.database import ShortsMasterDatabase
from app.services.pipeline import ShortsMasterPipeline
from app.services.research import ResearchService
from app.services.safety import SafetyGuard
from app.services.validation import EndToEndValidator
from tests.conftest import make_test_config
from tests.test_validation_quality import specific_world_cup_script


def test_research_brief_requires_three_facts() -> None:
    brief = ResearchBrief(
        topic="world cup schedule",
        category="sports",
        why_now="Google Trends shows demand.",
        concrete_facts=["One fact.", "Two facts."],
    )

    assert not brief.has_enough_facts
    assert "2/3" in brief.missing_facts_reason()


def test_wikipedia_relevance_rejects_wrong_sport_page() -> None:
    service = ResearchService({"research": {}})

    assert not service._is_relevant_wikipedia_result(
        "texas softball",
        "Texas Football League",
        "The Texas Football League was an American football minor league in the United States.",
    )
    assert service._is_relevant_wikipedia_result(
        "world cup schedule",
        "2026 FIFA World Cup",
        "The 2026 FIFA World Cup will be hosted by the United States, Canada, and Mexico.",
    )


def test_missing_research_facts_sets_needs_research(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db_path = tmp_path / "bot.db"
    config["app"]["database_path"] = str(db_path)
    pipeline = ShortsMasterPipeline(config)
    topic = TrendTopic(source="google_trends", title="thin topic", score=100)
    item = pipeline.db.enqueue_topic(topic, QueueStatus.APPROVED)
    pipeline.research.build_brief = lambda _topic: ResearchBrief(
        topic="thin topic",
        category="general",
        why_now="No useful context found.",
        concrete_facts=["Only one concrete fact."],
        uncertainty_notes=["Missing public context."],
    )

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.NEEDS_RESEARCH
    assert "1/3" in result["upload_blocked_reason"]
    assert result["video_path"] is None


def test_stale_sports_topic_sets_needs_fresh_source(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db_path = tmp_path / "bot.db"
    config["app"]["database_path"] = str(db_path)
    pipeline = ShortsMasterPipeline(config)
    topic = TrendTopic(source="google_trends", title="Texas softball vs Nebraska", score=100, niche="sports")
    item = pipeline.db.enqueue_topic(topic, QueueStatus.APPROVED)
    stale_date = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
    pipeline.research.build_brief = lambda _topic: ResearchBrief(
        topic=topic.title,
        category="sports",
        why_now="A dated sports source exists, but it is stale.",
        concrete_facts=[
            "Texas faces Nebraska in the NCAA softball tournament.",
            "The matchup is part of the Women's College World Series.",
            "The game involves a semifinal berth.",
        ],
        source_summaries=[
            {
                "source": "Example Sports",
                "title": "Texas softball vs Nebraska",
                "summary": "Texas faces Nebraska in the NCAA softball tournament.",
                "url": "https://example.test/sports",
                "source_date": stale_date,
            }
        ],
        source_dates=[
            {
                "source": "Example Sports",
                "title": "Texas softball vs Nebraska",
                "url": "https://example.test/sports",
                "source_date": stale_date,
            }
        ],
        freshness_score=20.0,
        freshness_threshold=70.0,
        freshness_window_hours=72,
        requires_fresh_source=True,
        freshness_notes=["newest dated source is stale"],
    )

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.NEEDS_FRESH_SOURCE
    assert "freshness score 20.0/70.0" in result["upload_blocked_reason"]
    assert result["video_path"] is None


def test_recent_sports_topic_passes_freshness_gate(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db_path = tmp_path / "bot.db"
    config["app"]["database_path"] = str(db_path)
    pipeline = ShortsMasterPipeline(config)
    topic = TrendTopic(source="google_trends", title="Texas softball vs Nebraska", score=100, niche="sports")
    item = pipeline.db.enqueue_topic(topic, QueueStatus.APPROVED)
    recent_date = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat()
    pipeline.research.build_brief = lambda _topic: ResearchBrief(
        topic=topic.title,
        category="sports",
        why_now="A recent sports source confirms the matchup context.",
        concrete_facts=[
            "Texas faces Nebraska in the NCAA softball tournament.",
            "The matchup is part of the Women's College World Series.",
            "The game involves a semifinal berth.",
        ],
        source_summaries=[
            {
                "source": "Example Sports",
                "title": "Texas softball vs Nebraska",
                "summary": "Texas faces Nebraska in the NCAA softball tournament.",
                "url": "https://example.test/sports",
                "source_date": recent_date,
            }
        ],
        source_dates=[
            {
                "source": "Example Sports",
                "title": "Texas softball vs Nebraska",
                "url": "https://example.test/sports",
                "source_date": recent_date,
            }
        ],
        freshness_score=98.0,
        freshness_threshold=70.0,
        freshness_window_hours=72,
        requires_fresh_source=True,
        freshness_notes=["fresh dated source found 4.0 hours old"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"paper mode does not upload this")
    voice_path = tmp_path / "voice.mp3"
    voice_path.write_bytes(b"paper mode test audio")
    pipeline.scripts.generate = lambda _topic, _research: texas_softball_script()
    pipeline.voice.generate = lambda _script, _queue_id: voice_path
    pipeline.images.generate = lambda _script, _topic, _queue_id: []
    pipeline.video.assemble = lambda _script, _images, _voice, _queue_id: video_path

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.PAPER_PUBLISHED
    assert result["youtube_video_id"].startswith(f"paper-{item['id']}-")


def test_recent_low_trust_sports_source_blocks(tmp_path: Path) -> None:
    pipeline, item, topic = _sports_pipeline_with_topic(tmp_path)
    pipeline.research.build_brief = lambda _topic: _sports_brief(
        topic.title,
        source="Unknown Hoops Blog",
        url="https://example-blog.test/post",
        source_trust_score=35.0,
        trust_score=35.0,
        trust_notes=["unknown blog source"],
    )

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.NEEDS_TRUSTED_SOURCE
    assert "trust score 35.0/70.0" in result["upload_blocked_reason"]
    assert result["video_path"] is None


def test_official_recent_sports_source_passes_trust_gate(tmp_path: Path) -> None:
    pipeline, item, topic = _sports_pipeline_with_topic(tmp_path)
    pipeline.research.build_brief = lambda _topic: _sports_brief(
        topic.title,
        source="NCAA",
        url="https://www.ncaa.com/news/softball",
        source_trust_score=96.0,
        trust_score=96.0,
        trust_notes=["trusted source requirement passed; best source score 96.0"],
    )
    _mock_media_pipeline(pipeline, tmp_path)

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.PAPER_PUBLISHED
    assert result["youtube_video_id"].startswith(f"paper-{item['id']}-")


def test_reddit_only_current_sports_topic_requires_trusted_source(tmp_path: Path) -> None:
    pipeline, item, topic = _sports_pipeline_with_topic(tmp_path, source="reddit")
    pipeline.research.build_brief = lambda _topic: _sports_brief(
        topic.title,
        source="Reddit",
        url="https://www.reddit.com/r/sports/comments/example",
        source_trust_score=25.0,
        trust_score=25.0,
        trust_notes=["social or community source is not trusted enough for current sports"],
    )

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.NEEDS_TRUSTED_SOURCE
    assert "requires an official or recognized sports source" in result["upload_blocked_reason"]


def test_wikipedia_only_current_sports_topic_requires_trusted_source(tmp_path: Path) -> None:
    pipeline, item, topic = _sports_pipeline_with_topic(tmp_path)
    pipeline.research.build_brief = lambda _topic: _sports_brief(
        topic.title,
        source="Wikipedia",
        url="https://en.wikipedia.org/wiki/Texas_Longhorns_softball",
        source_trust_score=45.0,
        trust_score=45.0,
        trust_notes=["Wikipedia is context only, not a trusted current sports source"],
    )

    result = pipeline.process_item(item)

    assert result["status"] == QueueStatus.NEEDS_TRUSTED_SOURCE
    assert "trust score 45.0/70.0" in result["upload_blocked_reason"]


def _sports_pipeline_with_topic(
    tmp_path: Path,
    source: str = "google_trends",
) -> tuple[ShortsMasterPipeline, dict, TrendTopic]:
    config = make_test_config(tmp_path)
    db_path = tmp_path / "bot.db"
    config["app"]["database_path"] = str(db_path)
    pipeline = ShortsMasterPipeline(config)
    topic = TrendTopic(source=source, title="Texas softball vs Nebraska", score=100, niche="sports")
    item = pipeline.db.enqueue_topic(topic, QueueStatus.APPROVED)
    return pipeline, item, topic


def _sports_brief(
    topic: str,
    source: str,
    url: str,
    source_trust_score: float,
    trust_score: float,
    trust_notes: list[str],
) -> ResearchBrief:
    recent_date = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat()
    source_payload = {
        "source": source,
        "title": topic,
        "summary": "Texas faces Nebraska in the NCAA softball tournament.",
        "url": url,
        "source_date": recent_date,
        "source_trust_score": source_trust_score,
    }
    return ResearchBrief(
        topic=topic,
        category="sports",
        why_now="A recent dated source confirms the sports context.",
        concrete_facts=[
            "Texas faces Nebraska in the NCAA softball tournament.",
            "The matchup is part of the Women's College World Series.",
            "The game involves a semifinal berth.",
        ],
        source_summaries=[source_payload],
        source_dates=[source_payload],
        freshness_score=98.0,
        freshness_threshold=70.0,
        freshness_window_hours=72,
        requires_fresh_source=True,
        freshness_notes=["fresh dated source found 4.0 hours old"],
        trust_score=trust_score,
        trust_threshold=70.0,
        requires_trusted_source=True,
        trust_notes=trust_notes,
    )


def _mock_media_pipeline(pipeline: ShortsMasterPipeline, tmp_path: Path) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"paper mode does not upload this")
    voice_path = tmp_path / "voice.mp3"
    voice_path.write_bytes(b"paper mode test audio")
    pipeline.scripts.generate = lambda _topic, _research: texas_softball_script()
    pipeline.voice.generate = lambda _script, _queue_id: voice_path
    pipeline.images.generate = lambda _script, _topic, _queue_id: []
    pipeline.video.assemble = lambda _script, _images, _voice, _queue_id: video_path


def texas_softball_script() -> ContentScript:
    narration = (
        "Texas softball tem uma pista de semifinal. O detalhe estranho é que esse confronto pode virar antes do primeiro arremesso. "
        "Primeira pista: Texas enfrenta Nebraska no torneio da NCAA. Depois fica mais claro: o jogo se conecta à fase decisiva universitária feminina. "
        "O detalhe que muita gente perde: uma vaga na semifinal está em jogo. Isso aumenta a tensão: cada troca de arremessadora pode virar a chave. "
        "Um erro pode transformar uma temporada longa em uma última entrada. Aqui vem a revelação: não é só Texas contra Nebraska; "
        "é pressão, descanso e sobrevivência em um confronto. Final rápido: lembre da pista, pressão de semifinal muda tudo."
    )
    scenes = [
        {"caption": "Pista de semifinal", "image_prompt": "visual vertical Texas softball Nebraska em estádio, sem logos"},
        {"caption": "Antes do arremesso", "image_prompt": "visual vertical Texas softball em dugout tenso antes de Nebraska"},
        {"caption": "Texas vs Nebraska", "image_prompt": "vertical generated Texas softball versus Nebraska scoreboard style no readable text"},
        {"caption": "Torneio NCAA", "image_prompt": "visual vertical chave de torneio NCAA softball, sem logos"},
        {"caption": "Vaga em jogo", "image_prompt": "visual vertical estádio de softball com clima de decisão"},
        {"caption": "Pressão na base", "image_prompt": "visual vertical base de softball com pressão Texas Nebraska"},
        {"caption": "Troca no arremesso", "image_prompt": "visual vertical troca de arremessadora em alta pressão"},
        {"caption": "Chave vira", "image_prompt": "visual vertical chave de softball virando sem texto"},
        {"caption": "A revelação", "image_prompt": "visual vertical Texas softball Nebraska em momento decisivo"},
        {"caption": "Pressão muda tudo", "image_prompt": "visual vertical entrada final de softball sob pressão"},
    ]
    return ContentScript(
        title="Texas Softball vs Nebraska: a pista da semifinal",
        hook="Texas softball tem uma pista de semifinal.",
        narration=narration,
        scenes=scenes,
        tags=["texas", "nebraska", "softball", "ncaa"],
        description="Vídeo curto factual em português sobre Texas softball contra Nebraska e a pressão da semifinal.",
        niche="sports",
    )


def test_generic_script_is_blocked_by_safety(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    script = ContentScript(
        title="Calendário da Copa: por que está em alta",
        hook="Calendário da Copa está em alta.",
        narration=(
            "O calendário da Copa está em alta. Aqui está o sinal para observar. "
            "Todo mundo está falando porque o tema está ganhando atenção. Isso é importante "
            "porque o assunto anda rápido, mas os detalhes ainda estão vagos."
        ),
        scenes=[
            {"caption": "Tendência", "image_prompt": "fundo abstrato de tendência"},
            {"caption": "Atenção", "image_prompt": "fundo de rede social"},
            {"caption": "Contexto", "image_prompt": "fundo de explicação"},
            {"caption": "Recompensa", "image_prompt": "fundo de audiência"},
        ],
        tags=["viralbr"],
        description="Explicação genérica sobre a alta do calendário da Copa.",
        niche="sports",
    )

    decision = SafetyGuard(db, config).evaluate_content(
        1,
        TrendTopic(source="google_trends", title="calendário da copa do mundo", score=100),
        script,
    )

    assert not decision["allowed"]
    assert any("generic fallback" in reason for reason in decision["reasons"])


def test_specific_researched_script_scores_above_75() -> None:
    brief = ResearchBrief(
        topic="calendário da copa do mundo",
        category="sports",
        why_now="A busca atual mostra demanda pelo calendário da Copa do Mundo de 2026.",
        concrete_facts=[
            "A Copa do Mundo FIFA de 2026 cresce para 48 times.",
            "O torneio será sediado por Estados Unidos, Canadá e México.",
            "O calendário inclui fase de grupos antes das eliminatórias.",
            "As eliminatórias incluem oitavas, quartas, semifinais e final.",
        ],
        names_entities=["FIFA", "Estados Unidos", "Canadá", "México"],
        dates_times=["2026"],
        locations=["Estados Unidos", "Canadá", "México"],
        source_urls=["https://en.wikipedia.org/wiki/2026_FIFA_World_Cup"],
    )

    result = EndToEndValidator.__new__(EndToEndValidator)._analyze_quality(
        specific_world_cup_script(),
        TrendTopic(source="google_trends", title="calendário da copa do mundo", score=500),
        {"duration_seconds": 60.0},
        {"duration_seconds": 60.0},
        [{"source": "pollinations"} for _ in range(10)],
        brief,
    )

    assert result["final_quality_score"] > 75
    assert len(result["score_breakdown"]["specificity"]["used_research_facts"]) >= 3
