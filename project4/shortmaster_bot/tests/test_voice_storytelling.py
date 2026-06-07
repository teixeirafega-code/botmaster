from __future__ import annotations

from app.generators.voice import VOICE_PROFILES, VoiceGenerator
from app.models import ContentScript
from app.services.voice_quality import score_voice_quality

from conftest import make_test_config


def make_script() -> ContentScript:
    narration = (
        "Pessoal do Reddit: quando seu instinto mandou você sair correndo? "
        "Eu cheguei ao hotel pouco antes da meia-noite. "
        "Até que ele apareceu segurando uma navalha. "
        "Peguei minhas chaves e fui embora sem olhar para trás."
    )
    return ContentScript(
        title="Uma noite estranha",
        hook="Pessoal do Reddit: quando seu instinto mandou você sair correndo?",
        narration=narration,
        scenes=[{"caption": narration, "image_prompt": "story"}] * 8,
        tags=["historiasreddit", "relatos", "shortsbr"],
        description="Uma história curta em português.",
        niche="reddit_story",
    )


def test_story_voice_profiles_use_distinct_pt_br_neural_voices() -> None:
    assert VOICE_PROFILES["male_storyteller"]["voice"] == "pt-BR-AntonioNeural"
    assert VOICE_PROFILES["female_storyteller"]["voice"] == "pt-BR-FranciscaNeural"
    assert VOICE_PROFILES["neutral_storyteller"]["voice"] == "pt-BR-ThalitaMultilingualNeural"
    assert len({profile["voice"] for profile in VOICE_PROFILES.values()}) == 3


def test_story_segments_add_suspense_pause_and_prosody(tmp_path) -> None:
    generator = VoiceGenerator(make_test_config(tmp_path))
    profile = generator._profile("male_storyteller")
    segments = generator._story_segments(make_script().narration, profile)

    assert segments[0]["role"] == "hook"
    suspense = next(segment for segment in segments if "navalha" in segment["text"])
    assert suspense["role"] == "suspense"
    assert suspense["pause_after_seconds"] >= 0.35
    assert suspense["rate"] != profile["rate"]
    assert suspense["pitch"] != profile["pitch"]


def test_voice_quality_scores_neural_storytelling_above_robotic_fallback() -> None:
    script = make_script()
    neural = score_voice_quality(
        script,
        duration_seconds=36,
        pause_ratio=0.12,
        dynamic_variation=0.28,
        generation_report={
            "provider": "edge_tts",
            "voice_profile": "female_storyteller",
            "voice_name": "pt-BR-FranciscaNeural",
            "segment_count": 4,
            "emotional_variation_count": 4,
            "suspense_segment_count": 1,
            "suspense_pause_count": 1,
        },
    )
    fallback = score_voice_quality(
        script,
        duration_seconds=25,
        pause_ratio=0.01,
        dynamic_variation=0.02,
        generation_report={
            "provider": "gtts",
            "voice_profile": "neutral_storyteller",
            "voice_name": "gTTS",
            "segment_count": 1,
            "emotional_variation_count": 0,
            "suspense_segment_count": 1,
            "suspense_pause_count": 0,
        },
    )

    assert neural["voice_naturalness_score"] > fallback["voice_naturalness_score"]
    assert neural["voice_engagement_score"] > fallback["voice_engagement_score"]
    assert neural["narration_pacing_score"] > fallback["narration_pacing_score"]
