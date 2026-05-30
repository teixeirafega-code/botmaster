from app.config.settings import PricingSettings
from app.economics.models import ValuationFactors, ValuationResult
from app.economics.pricing import DynamicPricingEngine
from app.models import DomainCandidate


def test_price_for_score_bands():
    pricing = PricingSettings()
    assert pricing.price_for_score(60) == 200
    assert pricing.price_for_score(70) == 500
    assert pricing.price_for_score(80) == 1500
    assert pricing.price_for_score(90) == 5000


def test_smart_price_uses_keyword_backlink_age_and_comps():
    factors = ValuationFactors(
        comparable_sales=0.5,
        commercial_intent=0.5,
        cpc_value=0.5,
        search_demand=0.5,
        extension_quality=1,
        linguistic_quality=0.8,
        brandability=0.8,
        length_quality=0.8,
        pronounceability=0.8,
        trend_momentum=0.7,
        seo_authority=0.7,
        backlink_quality=0.5,
        spam_safety=0.9,
        trademark_safety=0.9,
        archive_quality=0.7,
        liquidity_probability=0.5,
    )
    valuation = ValuationResult(
        domain="aicloud.com",
        score=85,
        fair_market_value=1000,
        expected_resale_probability=0.6,
        estimated_holding_days=90,
        expected_sale_price=600,
        expected_roi=10,
        liquidity_adjusted_roi=5,
        time_adjusted_roi=1,
        purchase_confidence=0.8,
        recommended_purchase_price=12,
        recommended_list_price=1000,
        niche="ai",
        extension=".com",
        factors=factors,
        market_signals={"namebio_comparable_median": 2000},
    )

    price = DynamicPricingEngine().smart_price(
        DomainCandidate(name="aicloud.com", source="test", keyword_value=20, backlinks=500, age_years=10),
        valuation,
    )

    assert price > valuation.recommended_list_price
