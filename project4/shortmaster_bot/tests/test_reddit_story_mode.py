from __future__ import annotations

from pathlib import Path

from app.generators.script import ScriptGenerator
from app.generators.video import VideoAssembler
from app.models import ContentScript, TrendTopic
from app.services.database import ShortsMasterDatabase
from app.services.narrative_style import analyze_reddit_narrative_style
from app.services.reddit_story import RedditStoryService
from app.services.safety import SafetyGuard
from app.services.validation import EndToEndValidator
from tests.conftest import make_test_config


def story_config(tmp_path: Path) -> dict:
    config = make_test_config(tmp_path)
    config["story_mode"] = {
        "enabled": True,
        "subreddits": ["AskReddit", "todayilearned", "LetsNotMeet", "NoStupidQuestions", "LifeProTips", "tifu", "interestingasfuck"],
        "min_upvotes": 1000,
        "min_comments": 50,
        "min_comment_score": 100,
        "min_story_chars": 180,
        "min_story_words": 35,
        "use_top_comments": False,
        "requires_fresh_source": False,
        "freshness_window_hours": 168,
        "freshness_min_score": 70,
        "trust_min_score": 70,
        "background_video": {
            "mode": "retention_categories",
            "royalty_free_only": True,
            "loop": True,
            "subtitles": "always",
            "loop_seconds": 8,
            "subtitle_words_per_chunk": 7,
            "forced_category": "deep_cleaning",
            "approved_categories": {
                "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
                "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
            },
        },
    }
    config["safety"]["source_copy_ngram_words"] = 8
    return config


def source_text() -> str:
    return (
        "I thought the basement noise was the old heater, so I ignored it for weeks. "
        "One night the sound moved from the pipes to the locked storage room. "
        "The next morning a chair was facing the door even though nobody used that room. "
        "I checked the window latch and noticed fresh scratches near the frame. "
        "After that, the family camera showed a stranger standing still in the yard just before sunrise."
    )


def test_story_source_rejects_low_engagement_and_selects_priority_story(tmp_path: Path) -> None:
    service = RedditStoryService(story_config(tmp_path))
    low = {
        "id": "low1",
        "title": "A creepy thing happened in my basement",
        "selftext": source_text(),
        "ups": 99,
        "num_comments": 3,
        "upvote_ratio": 0.92,
        "created_utc": 1_800_000_000,
        "permalink": "/r/LetsNotMeet/comments/low1/test/",
    }
    high = {
        **low,
        "id": "high1",
        "ups": 4500,
        "num_comments": 620,
        "permalink": "/r/LetsNotMeet/comments/high1/test/",
    }

    assert service._candidate_from_post(low, "LetsNotMeet") is None
    candidate = service._candidate_from_post(high, "LetsNotMeet")

    assert candidate is not None
    assert candidate.rejection_reasons == []
    assert "scary" in candidate.priority_labels
    assert candidate.selection_score > 0


def test_story_script_is_natural_and_keeps_source_safety_internal(tmp_path: Path) -> None:
    service = RedditStoryService(story_config(tmp_path))
    candidate = service._candidate_from_post(
        {
            "id": "abc123",
            "title": "A creepy thing happened in my basement",
            "selftext": source_text(),
            "ups": 4500,
            "num_comments": 620,
            "upvote_ratio": 0.94,
            "created_utc": 1_800_000_000,
            "permalink": "/r/LetsNotMeet/comments/abc123/test/",
        },
        "LetsNotMeet",
    )
    assert candidate is not None
    topic = service.topic_from_candidate(candidate)
    script = ScriptGenerator(story_config(tmp_path)).generate(topic, service.build_research_brief(topic))
    report = service.story_source_report(topic)

    style = analyze_reddit_narrative_style(script)

    assert script.narration.startswith("Pessoal do Reddit:")
    assert "eu " in script.narration.lower()
    assert style["narrative_naturalness_score"] >= 75
    assert style["disclaimer_leakage_score"] == 0
    assert report["raw_source_text_included"] is False
    assert "unverified" in report["fiction_framing_policy"]
    assert "I thought the basement noise was the old heater" not in script.narration


def story_script(verbatim: bool = False, disclaimer: bool = False, report_like: bool = False) -> ContentScript:
    copied = "I thought the basement noise was the old heater so ignored it for weeks. " if verbatim else ""
    leaked = " Vale ressaltar que este relato não foi confirmado." if disclaimer else ""
    report_line = " Curiosidade: aqui vem a revelação da fonte." if report_like else ""
    narration = (
        "Pessoal do Reddit: qual barulho em casa fez você congelar? "
        + copied
        + "Durante semanas, eu ouvi pancadas no porão e culpei o aquecedor velho. "
        "Até que o som mudou de lugar. Ele saiu dos canos e foi parar atrás da porta do depósito. "
        "Na manhã seguinte, uma cadeira estava virada para a entrada. Ninguém usava aquele cômodo. "
        "Eu tentei rir, mas encontrei riscos novos na trava da janela. "
        "Naquela noite, tranquei tudo e deixei a câmera ligada. "
        "Pouco antes do amanhecer, meu celular vibrou. A gravação mostrava um homem parado no quintal. "
        "Ele olhava direto para a janela do porão. Chamei a polícia e esperei no carro do vizinho. "
        "Quando entraram no depósito, acharam comida, cobertores e marcas de sapato. "
        "O aquecedor nunca tinha feito barulho. Alguém estava morando ali embaixo."
        + leaked
        + report_line
    )
    scenes = [
        {"caption": "Pancadas no porão", "image_prompt": "vídeo satisfatório acompanhando mistério no porão"},
        {"caption": "O som mudou", "image_prompt": "vídeo satisfatório acompanhando porta do depósito"},
        {"caption": "A cadeira virou", "image_prompt": "vídeo satisfatório acompanhando cadeira fora do lugar"},
        {"caption": "Riscos na janela", "image_prompt": "vídeo satisfatório acompanhando janela arranhada"},
        {"caption": "Câmera ligada", "image_prompt": "vídeo satisfatório acompanhando câmera noturna"},
        {"caption": "Alguém no quintal", "image_prompt": "vídeo satisfatório acompanhando sombra no quintal"},
        {"caption": "Olhando para o porão", "image_prompt": "vídeo satisfatório acompanhando janela do porão"},
        {"caption": "A polícia entrou", "image_prompt": "vídeo satisfatório acompanhando porta aberta"},
        {"caption": "Cobertores escondidos", "image_prompt": "vídeo satisfatório acompanhando objetos escondidos"},
        {"caption": "Morando ali embaixo", "image_prompt": "vídeo satisfatório acompanhando descoberta final"},
    ]
    return ContentScript(
        title="O barulho no porão não vinha dos canos",
        hook="Pessoal do Reddit: qual barulho em casa fez você congelar?",
        narration=narration,
        scenes=scenes,
        tags=["historiasreddit", "relatos", "shortsbr"],
        description="Um barulho tratado como defeito da casa termina com uma descoberta assustadora.",
        niche="reddit_story",
    )


def test_story_safety_blocks_verbatim_reddit_source_overlap(tmp_path: Path) -> None:
    config = story_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(
        source="reddit_story",
        title="Relato do Reddit em r/LetsNotMeet: pista no porão",
        score=100,
        niche="reddit_story",
        raw={
            "content_mode": "reddit_story",
            "source_text_for_similarity": source_text(),
            "story_source": {"source_kind": "post_selftext"},
        },
    )

    blocked = SafetyGuard(db, config).evaluate_content(1, topic, story_script(verbatim=True))
    allowed = SafetyGuard(db, config).evaluate_content(2, topic, story_script(verbatim=False))

    assert blocked["allowed"] is False
    assert any("copied too closely" in reason for reason in blocked["reasons"])
    assert allowed["allowed"] is True


def test_story_safety_blocks_public_disclaimer_leakage(tmp_path: Path) -> None:
    config = story_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(
        source="reddit_story",
        title="Relato do Reddit em r/LetsNotMeet: pista no porão",
        score=100,
        niche="reddit_story",
        raw={"content_mode": "reddit_story", "source_text_for_similarity": source_text()},
    )

    decision = SafetyGuard(db, config).evaluate_content(1, topic, story_script(disclaimer=True))

    assert decision["allowed"] is False
    assert decision["checks"]["disclaimer_leakage_score"] > 0
    assert any("disclaimer" in reason for reason in decision["reasons"])


def test_story_safety_blocks_report_like_narration(tmp_path: Path) -> None:
    config = story_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    topic = TrendTopic(
        source="reddit_story",
        title="Relato do Reddit em r/LetsNotMeet: pista no porão",
        score=100,
        niche="reddit_story",
        raw={"content_mode": "reddit_story", "source_text_for_similarity": source_text()},
    )

    decision = SafetyGuard(db, config).evaluate_content(1, topic, story_script(report_like=True))

    assert decision["allowed"] is False
    assert decision["checks"]["narrative_naturalness_score"] < 75


def test_story_quality_report_includes_new_scores_and_retention_backgrounds(tmp_path: Path) -> None:
    validator = object.__new__(EndToEndValidator)
    validator.config = story_config(tmp_path)
    result = validator._analyze_quality(
        story_script(),
        TrendTopic(source="reddit_story", title="Relato do Reddit em r/LetsNotMeet: pista no porão", score=100, niche="reddit_story"),
        {"duration_seconds": 58.0},
        {
            "duration_seconds": 58.0,
            "background_mode": "retention_categories",
            "background_category": "deep_cleaning",
            "background_tier": "tier_1",
            "background_animated": True,
            "subtitles_enabled": True,
            "background_looped": True,
        },
        [{"source": "procedural_retention_background", "royalty_free": True, "background_category": "deep_cleaning", "animated": True}],
    )

    assert result["hook_score"] >= 70
    assert result["curiosity_score"] >= 70
    assert result["storytelling_score"] >= 70
    assert result["narrative_naturalness_score"] >= 75
    assert result["disclaimer_leakage_score"] == 0
    assert result["visual_interest_score"] >= 70
    assert result["score_breakdown"]["visual_interest"]["background_category"] == "deep_cleaning"


def test_video_assembler_uses_story_retention_background(tmp_path: Path) -> None:
    assembler = VideoAssembler(story_config(tmp_path))

    assert assembler.uses_story_background is True
    assert assembler.story_background_config["mode"] == "retention_categories"
    assert assembler._select_background_category() == "deep_cleaning"
