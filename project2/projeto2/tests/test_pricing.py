from app.config.settings import PricingSettings


def test_price_for_score_bands():
    pricing = PricingSettings()
    assert pricing.price_for_score(60) == 200
    assert pricing.price_for_score(70) == 500
    assert pricing.price_for_score(80) == 1500
    assert pricing.price_for_score(90) == 5000

