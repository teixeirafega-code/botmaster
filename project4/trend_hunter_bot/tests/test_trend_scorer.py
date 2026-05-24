from app.analyzers.trend_scorer import TrendScorer
from app.config.settings import ScoringSettings
from app.models import TrendSignal


def test_trend_scorer_scores_cross_platform_commercial_trend() -> None:
    scorer = TrendScorer(ScoringSettings())
    signals = [
        TrendSignal(
            name="AI launch tool",
            platform="google_trends",
            growth_velocity=225,
            search_volume=95,
            social_engagement=0,
            keywords=("ai", "launch", "tool"),
        ),
        TrendSignal(
            name="AI launch tool",
            platform="twitter",
            growth_velocity=180,
            search_volume=0,
            social_engagement=8000,
            keywords=("ai", "startup", "new"),
        ),
    ]

    trend = scorer.score_signals(signals)[0]

    assert trend.score >= 70
    assert trend.component_scores["growth_velocity"] > 70
    assert trend.component_scores["commercial_potential"] > 20
    assert trend.platforms == ("google_trends", "twitter")


def test_group_signals_ignores_empty_normalized_names() -> None:
    scorer = TrendScorer(ScoringSettings())
    grouped = scorer.group_signals([TrendSignal(name="!!!", platform="test")])

    assert grouped == {}

