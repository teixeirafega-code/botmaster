from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config.settings import load_settings
from app.scheduler import BotScheduler
from app.services.asset_manager import AssetManager
from app.services.profit_tracker import ProfitTracker
from app.utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset-flip-bot",
        description="Monitor digital asset marketplaces for undervalued resale opportunities.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to .env",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run the 24/7 scheduler")
    subparsers.add_parser("scheduler", help="Run the 24/7 scheduler")
    subparsers.add_parser("scan-once", help="Run one scrape/analyze/alert cycle")
    subparsers.add_parser("dashboard", help="Show CLI dashboard metrics")
    subparsers.add_parser("config-check", help="Validate configuration and show active markets")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config, args.env)
    configure_logging(settings.log_dir, settings.log_level)

    if args.command in {"run", "scheduler"}:
        BotScheduler(settings).run_forever()
        return 0
    if args.command == "scan-once":
        summary = AssetManager(settings).scan_once()
        print(
            f"Scan complete: {summary.assets_monitored} assets, "
            f"{summary.opportunities_found} opportunities, "
            f"${summary.total_potential_profit:,.0f} potential profit."
        )
        return 0
    if args.command == "dashboard":
        print_dashboard(ProfitTracker(settings.stats_path).load())
        return 0
    if args.command == "config-check":
        print_config(settings)
        return 0
    return 1


def print_config(settings: object) -> None:
    data = {
        "paper_mode": settings.paper_mode,
        "scan_interval_minutes": settings.scan_interval_minutes,
        "min_score_alert": settings.min_score_alert,
        "undervalued_threshold": settings.undervalued_threshold,
        "telegram_enabled": bool(settings.telegram and settings.telegram.enabled),
        "marketplaces": [
            {
                "name": marketplace.name,
                "enabled": marketplace.enabled,
                "urls": marketplace.urls,
                "cookie_env": marketplace.cookie_env,
            }
            for marketplace in settings.marketplaces
        ],
    }
    print(json.dumps(data, indent=2))


def print_dashboard(stats: dict[str, object]) -> None:
    opportunities = list(stats.get("opportunities", []))[:10]
    print("=" * 78)
    print("ASSET FLIP BOT DASHBOARD")
    print("=" * 78)
    print(f"Assets monitored:       {stats.get('assets_monitored', 0)}")
    print(f"Opportunities found:    {stats.get('opportunities_found', 0)}")
    print(f"Total potential profit: ${float(stats.get('total_potential_profit', 0.0)):,.0f}")
    print(f"Last scan:              {stats.get('last_scan_at') or 'never'}")
    print("-" * 78)
    if not opportunities:
        print("No opportunities recorded yet. Run `python -m app.main scan-once` first.")
        return

    print(f"{'Score':<7} {'Marketplace':<16} {'Price':>12} {'Value':>12} {'Asset':<25}")
    print("-" * 78)
    for item in opportunities:
        listing = item.get("listing", {})
        valuation = item.get("valuation", {})
        name = str(listing.get("name", ""))[:25]
        print(
            f"{item.get('opportunity_score', 0):<7} "
            f"{str(listing.get('marketplace', ''))[:16]:<16} "
            f"${float(listing.get('asking_price', 0.0)):>11,.0f} "
            f"${float(valuation.get('estimated_real_value', 0.0)):>11,.0f} "
            f"{name:<25}"
        )


if __name__ == "__main__":
    raise SystemExit(main())



