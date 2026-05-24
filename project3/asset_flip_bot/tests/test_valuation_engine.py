from app.analyzers.revenue_multiplier import RevenueMultiplier
from app.analyzers.valuation_engine import ValuationEngine
from app.models import AssetType, MarketplaceListing


def test_website_valuation_uses_30_to_40_monthly_cashflow_multiple() -> None:
    listing = MarketplaceListing(
        marketplace="test",
        external_id="1",
        name="Profitable content website",
        url="https://example.com/listing/1",
        asset_type=AssetType.WEBSITE,
        asking_price=15_000,
        monthly_revenue=1_000,
        monthly_profit=1_000,
    )

    valuation = ValuationEngine(RevenueMultiplier()).estimate(listing)

    assert valuation.low_value == 30_000
    assert valuation.high_value == 40_000
    assert valuation.estimated_real_value == 35_000
    assert valuation.profit_potential == 20_000
    assert valuation.discount_to_value == 0.4286


def test_youtube_valuation_uses_channel_multiple() -> None:
    listing = MarketplaceListing(
        marketplace="test",
        external_id="2",
        name="YouTube channel in finance niche",
        url="https://example.com/listing/2",
        asset_type=AssetType.YOUTUBE,
        asking_price=10_000,
        monthly_revenue=500,
        monthly_profit=500,
    )

    valuation = ValuationEngine(RevenueMultiplier()).estimate(listing)

    assert valuation.low_value == 12_000
    assert valuation.high_value == 15_000
    assert valuation.estimated_real_value == 13_500

