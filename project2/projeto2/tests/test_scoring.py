import pytest

from app.analyzers.backlink_checker import BacklinkChecker
from app.analyzers.keyword_analyzer import KeywordAnalyzer
from app.analyzers.scorer import DomainScorer
from app.config.settings import Settings
from app.models import DomainCandidate


@pytest.mark.asyncio
async def test_score_stays_between_zero_and_hundred():
    settings = Settings(paper_mode=True)
    scorer = DomainScorer(settings, BacklinkChecker(settings), KeywordAnalyzer())
    candidate = await scorer.score(DomainCandidate(name="aicloud.com", source="test", age_years=10))
    assert 0 <= candidate.score <= 100
    assert candidate.extension_points == 10

