from app.analyzers.opportunity_scorer import OpportunityScorer
from app.models import AssetType, MarketplaceListing, Valuation


def test_scorer_flags_assets_below_half_estimated_value() -> None:
    listing = MarketplaceListing(
        marketplace="test",
        external_id="1",
        name="B2B SaaS website",
        url="https://example.com/listing/1",
        asset_type=AssetType.SAAS,
        asking_price=20_000,
        monthly_revenue=2_000,
        monthly_profit=2_000,
        age_months=36,
        monthly_traffic=50_000,
        niche="B2B SaaS",
    )
    valuation = Valuation(
        low_value=72_000,
        high_value=120_000,
        estimated_real_value=96_000,
        profit_potential=76_000,
        discount_to_value=0.2083,
        multiplier_low=36,
        multiplier_high=60,
    )

    scored = OpportunityScorer(
        undervalued_threshold=0.5,
        niche_bonus={"saas": 10},
    ).score(listing, valuation)

    assert scored.is_undervalued is True
    assert scored.opportunity_score >= 80
    assert any("Asking price" in reason for reason in scored.reasons)


def test_scorer_rejects_fairly_priced_assets() -> None:
    listing = MarketplaceListing(
        marketplace="test",
        external_id="2",
        name="Fairly priced app",
        url="https://example.com/listing/2",
        asset_type=AssetType.APP,
        asking_price=90_000,
        monthly_revenue=3_000,
    )
    valuation = Valuation(
        low_value=72_000,
        high_value=108_000,
        estimated_real_value=90_000,
        profit_potential=0,
        discount_to_value=1.0,
        multiplier_low=24,
        multiplier_high=36,
    )

    scored = OpportunityScorer().score(listing, valuation)

    assert scored.is_undervalued is False

