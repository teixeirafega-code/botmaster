from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from app.analyzers.backlink_checker import BacklinkChecker
from app.analyzers.keyword_analyzer import KeywordAnalyzer
from app.analyzers.scorer import DomainScorer
from app.config.settings import get_settings
from app.core.events import EventBus
from app.db.postgres import build_repository
from app.marketplaces.afternic import AfternicMarketplace
from app.marketplaces.dan import DanMarketplace
from app.marketplaces.godaddy_auctions import GoDaddyAuctionsMarketplace
from app.marketplaces.sedo import SedoMarketplace
from app.registrars.godaddy import GoDaddyRegistrar
from app.scheduler import start_scheduler
from app.scrapers.auction_sources import DropCatchScraper, GoDaddyAuctionsScraper, NameJetScraper, SnapNamesScraper
from app.scrapers.expireddomains import ExpiredDomainsScraper
from app.scrapers.whoisxml_expiring import WhoisXmlExpiringDomainsScraper
from app.services.domain_manager import DomainManager
from app.services.domain_sniper import DomainSniper
from app.services.profit_tracker import ProfitTracker
from app.services.risk_manager import RiskManager
from app.services.telegram_notifier import TelegramNotifier
from app.services.transaction_manager import TransactionManager
from app.utils.logger import setup_logging


async def build_manager() -> DomainManager:
    settings = get_settings()
    setup_logging(settings.log_file)
    database_url = settings.database.url.get_secret_value() if settings.database.url else None
    repository = build_repository(database_url)
    await repository.connect()
    await repository.init_schema()
    backlink_checker = BacklinkChecker(settings)
    scorer = DomainScorer(settings, backlink_checker, KeywordAnalyzer())
    notifier = TelegramNotifier(settings, repository)
    event_bus = EventBus()
    scrapers = [WhoisXmlExpiringDomainsScraper(settings), ExpiredDomainsScraper(settings)]
    if settings.scraper.godaddy_auctions_enabled:
        scrapers.append(GoDaddyAuctionsScraper(settings))
    if settings.scraper.namejet_enabled:
        scrapers.append(NameJetScraper(settings))
    if settings.scraper.snapnames_enabled:
        scrapers.append(SnapNamesScraper(settings))
    if settings.scraper.dropcatch_enabled:
        scrapers.append(DropCatchScraper(settings))
    return DomainManager(
        settings=settings,
        scrapers=scrapers,
        scorer=scorer,
        registrar=GoDaddyRegistrar(settings),
        marketplaces=[
            GoDaddyAuctionsMarketplace(settings),
            SedoMarketplace(settings),
            AfternicMarketplace(settings),
            DanMarketplace(settings),
        ],
        notifier=notifier,
        repository=repository,
        event_bus=event_bus,
        transaction_manager=TransactionManager(notifier, repository),
        risk_manager=RiskManager(settings, notifier, repository),
    )


async def dashboard() -> None:
    settings = get_settings()
    setup_logging(settings.log_file)
    manager = await build_manager()
    snapshot = ProfitTracker().snapshot(await manager.load_state())
    print("Domain Hunter Bot Dashboard")
    print("===========================")
    print(f"Domains monitored: {snapshot['domains_monitored']}")
    print(f"Registered:         {snapshot['registered']}")
    print(f"Sold:               {snapshot['sold']}")
    print(f"Total invested:     ${snapshot['total_invested']}")
    print(f"Total profit:       ${snapshot['total_profit']}")
    print(f"Portfolio value:    ${snapshot['total_portfolio_value']}")
    print(f"Paper mode:         {settings.paper_mode}")
    await manager.repository.close()


async def portfolio_report() -> None:
    manager = await build_manager()
    try:
        domains = await manager.load_state()
        tracker = ProfitTracker()
        snapshot = tracker.snapshot(domains)
        rows = tracker.domain_rows(domains)
        report_path = Path("data") / "portfolio_report.csv"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = ["domain", "status", "cost", "list_price", "sale_price", "days_listed", "roi", "marketplaces"]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        for domain in domains:
            if domain.status.value == "sold":
                await manager.notifier.send_sale_alert(domain)
        print("Portfolio")
        print("=========")
        print(f"Domains:          {snapshot['domains_monitored']}")
        print(f"Registered:       {snapshot['registered']}")
        print(f"Sold:             {snapshot['sold']}")
        print(f"Invested:         ${snapshot['total_invested']}")
        print(f"Portfolio value:  ${snapshot['total_portfolio_value']}")
        print(f"Profit realized:  ${snapshot['total_profit']}")
        print(f"CSV:              {report_path}")
    finally:
        await manager.repository.close()


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Domain Hunter Bot")
    parser.add_argument("command", choices=["run-once", "scheduler", "dashboard", "sniper", "reprice", "portfolio"], nargs="?", default="dashboard")
    parser.add_argument("--sniper-cycles", type=int, default=1, help="Number of 30-second sniper refresh cycles to run; use 0 for continuous mode.")
    args = parser.parse_args()

    if args.command == "dashboard":
        await dashboard()
        return
    if args.command == "portfolio":
        await portfolio_report()
        return

    manager = await build_manager()
    settings = get_settings()
    try:
        if args.command == "run-once":
            await manager.notifier.send_startup_alert()
            await manager.run_cycle()
        elif args.command == "scheduler":
            await start_scheduler(manager, settings.scheduler.interval_minutes, settings.scheduler.timezone)
        elif args.command == "sniper":
            await manager.notifier.send_startup_alert()
            cycles = None if args.sniper_cycles == 0 else args.sniper_cycles
            results = await DomainSniper(manager).monitor(cycles=cycles)
            for result in results:
                print(
                    f"{result.domain} | target={result.target_time.isoformat()} | attempts={result.attempts} | success={result.success}"
                )
        elif args.command == "reprice":
            repriced = await manager.reprice_stale_listings()
            print(f"Repriced domains: {len(repriced)}")
            for domain in repriced:
                print(f"{domain.name}: ${domain.asking_price}")
    except Exception as exc:
        await manager.notifier.send_error("Application command failed", exc, critical=True)
        raise
    finally:
        await manager.repository.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
