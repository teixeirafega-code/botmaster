from __future__ import annotations

import argparse
import asyncio

from app.analyzers.backlink_checker import BacklinkChecker
from app.analyzers.keyword_analyzer import KeywordAnalyzer
from app.analyzers.scorer import DomainScorer
from app.config.settings import get_settings
from app.core.events import EventBus
from app.db.postgres import build_repository
from app.marketplaces.afternic import AfternicMarketplace
from app.marketplaces.godaddy_auctions import GoDaddyAuctionsMarketplace
from app.marketplaces.sedo import SedoMarketplace
from app.registrars.godaddy import GoDaddyRegistrar
from app.scheduler import start_scheduler
from app.scrapers.expireddomains import ExpiredDomainsScraper
from app.scrapers.whoisxml_expiring import WhoisXmlExpiringDomainsScraper
from app.services.domain_manager import DomainManager
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
    return DomainManager(
        settings=settings,
        scrapers=[WhoisXmlExpiringDomainsScraper(settings), ExpiredDomainsScraper(settings)],
        scorer=scorer,
        registrar=GoDaddyRegistrar(settings),
        marketplaces=[GoDaddyAuctionsMarketplace(settings), SedoMarketplace(settings), AfternicMarketplace(settings)],
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
    print(f"Paper mode:         {settings.paper_mode}")
    await manager.repository.close()


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Domain Hunter Bot")
    parser.add_argument("command", choices=["run-once", "scheduler", "dashboard"], nargs="?", default="dashboard")
    args = parser.parse_args()

    if args.command == "dashboard":
        await dashboard()
        return

    manager = await build_manager()
    settings = get_settings()
    try:
        if args.command == "run-once":
            await manager.notifier.send_startup_alert()
            await manager.run_cycle()
        elif args.command == "scheduler":
            await start_scheduler(manager, settings.scheduler.interval_minutes, settings.scheduler.timezone)
    except Exception as exc:
        await manager.notifier.send_error("Application command failed", exc, critical=True)
        raise
    finally:
        await manager.repository.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
