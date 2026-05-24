from app.config.settings import MarketplaceSettings
from app.models import AssetType
from app.scrapers.flippa import FlippaScraper


def test_scraper_extracts_embedded_json_listing() -> None:
    html = """
    <html>
      <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "listing": {
                "id": "abc",
                "title": "Finance content website",
                "url": "/listings/abc",
                "askingPrice": "$20,000",
                "monthlyProfit": "$1,200",
                "age": "3 years",
                "monthlyTraffic": "50k",
                "niche": "Finance",
                "assetType": "website"
              }
            }
          }
        }
      </script>
    </html>
    """
    scraper = FlippaScraper(
        MarketplaceSettings(
            name="flippa",
            enabled=True,
            urls=["https://flippa.com/search"],
        )
    )

    listings = scraper.parse("https://flippa.com/search", html)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.name == "Finance content website"
    assert listing.asking_price == 20_000
    assert listing.monthly_profit == 1_200
    assert listing.age_months == 36
    assert listing.monthly_traffic == 50_000
    assert listing.asset_type == AssetType.WEBSITE

