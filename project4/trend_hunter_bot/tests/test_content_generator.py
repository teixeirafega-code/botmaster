from app.actions.content_generator import ContentGenerator
from app.config.settings import ContentSettings
from app.models import ScoredTrend, TrendSignal


def test_content_generator_returns_configured_channels() -> None:
    generator = ContentGenerator(ContentSettings(max_ideas=3, channels=("blog", "newsletter")))
    trend = ScoredTrend(
        name="AI launch tool",
        normalized_name="ai launch tool",
        score=82.5,
        component_scores={},
        signals=(TrendSignal(name="AI launch tool", platform="google_trends"),),
    )

    ideas = generator.generate(trend)

    assert len(ideas) == 3
    assert {idea.channel for idea in ideas}.issubset({"blog", "newsletter"})
    assert all("AI launch tool" in idea.title for idea in ideas)

