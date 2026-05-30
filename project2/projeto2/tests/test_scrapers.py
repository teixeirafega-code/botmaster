from app.config.settings import Settings
from app.domain_sources import candidates_from_csv_text
from app.scrapers.auction_sources import GoDaddyAuctionsScraper
from app.scrapers.expireddomains import ExpiredDomainsScraper
from app.scrapers.whoisxml_expiring import WhoisXmlExpiringDomainsScraper


def test_expireddomains_parser_extracts_unique_domains():
    scraper = ExpiredDomainsScraper(Settings())
    domains = scraper.parse("<table><tr><td>AlphaCloud.com</td></tr><tr><td>alphacloud.com</td></tr></table>")
    assert [domain.name for domain in domains] == ["alphacloud.com"]
    assert domains[0].source == "expireddomains_deleted"


def test_expireddomains_headers_use_configured_user_agents():
    scraper = ExpiredDomainsScraper(Settings(scraper={"user_agents": ["UA-1"]}))
    assert scraper._headers()["User-Agent"] == "UA-1"
    assert scraper._headers()["Referer"] == "https://www.expireddomains.net/deleted-domains/"


def test_expireddomains_next_page_uses_current_url():
    scraper = ExpiredDomainsScraper(Settings())
    assert scraper.next_page_url('<a href="/deleted-domains/?start=25">Next Page &raquo;</a>', "https://www.expireddomains.net/deleted-domains/") == (
        "https://www.expireddomains.net/deleted-domains/?start=25"
    )


def test_whoisxml_csv_parser_reads_dropped_domains():
    scraper = WhoisXmlExpiringDomainsScraper(Settings())
    domains = scraper.parse_feed(
        """
        domainName,eventType,createdDate
        FinanceData.net,dropped,2019-05-01
        NewLaunch.io,added,2026-05-23
        FinanceData.net,dropped,2019-05-01
        """
    )
    assert domains[0].name == "financedata.net"
    assert domains[0].source == "whoisxml_expiring"
    assert domains[0].age_years >= 6
    assert [domain.name for domain in domains] == ["financedata.net"]


def test_whoisxml_line_parser_applies_feed_tld():
    scraper = WhoisXmlExpiringDomainsScraper(Settings())
    domains = scraper.parse_text("AlphaCloud\nBetaData\n", source_url="2026-05-23-drop.com.csv")
    assert [domain.name for domain in domains] == ["alphacloud.com", "betadata.com"]


def test_auction_csv_parser_preserves_expiry_metadata():
    domains = candidates_from_csv_text(
        "Domain Name,Auction End Time,Backlinks,Age\nAlphaCloud.com,2026-05-30T12:00:00Z,42,8\n",
        "godaddy_auctions",
    )

    assert domains[0].name == "alphacloud.com"
    assert domains[0].source == "godaddy_auctions"
    assert domains[0].auction_end_at is not None
    assert domains[0].backlinks == 42
    assert domains[0].age_years == 8


def test_godaddy_scraper_parses_zip_payload():
    scraper = GoDaddyAuctionsScraper(Settings())
    payload = b"Domain Name,Auction End Time\nBetaData.net,2026-05-30T13:00:00Z\n"

    domains = scraper._parse_payload(payload, "text/csv", "feed.csv")

    assert [domain.name for domain in domains] == ["betadata.net"]
