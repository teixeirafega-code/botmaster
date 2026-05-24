import json

from app.config.settings import MarketplaceSettings
from app.models import AssetType
from app.scrapers.base import money_to_float
from app.scrapers.empireflippers import EmpireFlippersScraper


def build_scraper() -> EmpireFlippersScraper:
    return EmpireFlippersScraper(
        MarketplaceSettings(
            name="empireflippers",
            enabled=True,
            urls=["https://empireflippers.com/marketplace/"],
        )
    )


def test_money_parser_does_not_join_listing_number_to_monetization_label() -> None:
    value = money_to_float("Listing #1280916 Monetization Monthly Net Profit")

    assert value == 1_280_916
    assert value != 1_280_916_000_000


def test_empireflippers_api_parser_uses_structured_financial_fields() -> None:
    payload = {
        "data": {
            "listings": [
                {
                    "id": "961ed5dd-415b-4b8d-80e4-b1de3695a0b7",
                    "listing_number": 92901,
                    "public_title": "$158.5K Per Month Agency Business in the Business Niche",
                    "average_monthly_net_profit": 89435,
                    "average_monthly_gross_revenue": 158528,
                    "listing_price": 2504152,
                    "unpriced": False,
                    "first_made_money_at": "2024-01-01",
                    "monetizations": [{"monetization": "Agency"}],
                    "niches": [{"niche": "Business"}],
                    "combined_site_metrics": [{"unique_users": 12500, "page_views": 22000}],
                    "sites": [{"platform": "Other"}],
                }
            ]
        }
    }

    listings = build_scraper().parse(
        "https://api.empireflippers.com/api/v1/listings/list",
        json.dumps(payload),
    )

    assert len(listings) == 1
    listing = listings[0]
    assert listing.external_id == "961ed5dd-415b-4b8d-80e4-b1de3695a0b7"
    assert listing.asking_price == 2_504_152
    assert listing.monthly_revenue == 158_528
    assert listing.monthly_profit == 89_435
    assert listing.asset_type == AssetType.OTHER
    assert listing.monthly_traffic == 22_000
    assert listing.url == "https://empireflippers.com/listing/92901/"


def test_empireflippers_api_parser_maps_ecommerce_and_filters_bad_prices() -> None:
    payload = {
        "data": {
            "listings": [
                {
                    "id": "ok",
                    "listing_number": 93744,
                    "public_title": "$422.1K Per Month eCommerce Business",
                    "average_monthly_net_profit": 99399,
                    "average_monthly_gross_revenue": 422123,
                    "listing_price": 1900000,
                    "unpriced": False,
                    "monetizations": [{"monetization": "Amazon FBA"}],
                    "niches": [{"niche": "Supplements"}],
                },
                {
                    "id": "bad",
                    "listing_number": 1280916,
                    "public_title": "Bad HTML-derived listing",
                    "average_monthly_net_profit": 1_000,
                    "average_monthly_gross_revenue": 2_000,
                    "listing_price": 1_280_916_000_000,
                    "unpriced": False,
                    "monetizations": [{"monetization": "Content"}],
                    "niches": [{"niche": "Finance"}],
                },
            ]
        }
    }

    listings = build_scraper().parse(
        "https://api.empireflippers.com/api/v1/listings/list",
        json.dumps(payload),
    )

    assert len(listings) == 1
    assert listings[0].external_id == "ok"
    assert listings[0].asking_price == 1_900_000
    assert listings[0].asset_type == AssetType.ECOMMERCE

