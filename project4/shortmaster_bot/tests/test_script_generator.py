from __future__ import annotations

from pathlib import Path

from app.generators.script import ScriptGenerator
from app.models import TrendTopic
from tests.conftest import make_test_config


def test_local_script_generator_returns_valid_short(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    generator = ScriptGenerator(config)
    topic = TrendTopic(
        source="tiktok",
        title="#AIEditing",
        score=200,
        niche="technology",
        hashtags=["#AIEditing"],
    )

    script = generator.generate(topic)

    assert script.title
    assert script.hook
    assert len(script.narration.split()) >= 100
    assert len(script.scenes) >= 8
    assert all(scene["caption"] and scene["image_prompt"] for scene in script.scenes)
    assert all(len(scene["caption"].split()) <= 6 for scene in script.scenes)
