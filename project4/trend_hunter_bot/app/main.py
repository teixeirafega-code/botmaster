from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.config.settings import ConfigError, load_settings
from app.scheduler import TrendScheduler
from app.services.trend_manager import TrendManager, ensure_database
from app.utils.logger import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(config_path=args.config, env_path=args.env)
        configure_logging(settings.app.log_file, settings.app.log_level)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "config-check":
        db_path = ensure_database(settings)
        print(json.dumps(_settings_summary(settings, db_path), indent=2, default=str))
        return 0

    manager = TrendManager(settings)

    if args.command == "once":
        summary = manager.run_cycle()
        print(json.dumps(summary.to_dict(), indent=2))
        return 0

    if args.command == "dashboard":
        snapshot = manager.dashboard_snapshot(top_limit=args.limit)
        _print_dashboard(snapshot)
        return 0

    if args.command in {"run", "scheduler"}:
        scheduler = TrendScheduler(settings, manager=manager)
        scheduler.run_forever()
        return 0

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trend-hunter-bot",
        description="Monitor emerging trends, score opportunities, and act safely in paper mode.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--env", type=Path, default=None, help="Path to .env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="Run the 24/7 scheduler")
    subparsers.add_parser("scheduler", help="Run the 24/7 scheduler")
    subparsers.add_parser("once", help="Run a single collection and action cycle")
    dashboard = subparsers.add_parser("dashboard", help="Show CLI dashboard")
    dashboard.add_argument("--limit", type=int, default=10, help="Number of top trends to show")
    subparsers.add_parser("config-check", help="Validate configuration and initialize local state")
    return parser


def _print_dashboard(snapshot: dict[str, Any]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        _print_plain_dashboard(snapshot)
        return

    console = Console()
    console.print("[bold]Trend Hunter Bot Dashboard[/bold]")
    console.print(f"Trends detected today: {snapshot['trends_detected_today']}")
    console.print(f"Domains registered today: {snapshot['domains_registered_today']}")

    table = Table(title="Top Trending Now")
    table.add_column("Rank", justify="right")
    table.add_column("Trend")
    table.add_column("Score", justify="right")
    table.add_column("Platforms")
    table.add_column("Observed")
    for index, trend in enumerate(snapshot["top_trending_now"], start=1):
        table.add_row(
            str(index),
            str(trend["name"]),
            f"{float(trend['score']):.1f}",
            ", ".join(trend["platforms"]),
            str(trend["observed_at"]),
        )
    console.print(table)


def _print_plain_dashboard(snapshot: dict[str, Any]) -> None:
    print("Trend Hunter Bot Dashboard")
    print(f"Trends detected today: {snapshot['trends_detected_today']}")
    print(f"Domains registered today: {snapshot['domains_registered_today']}")
    print("Top Trending Now")
    for index, trend in enumerate(snapshot["top_trending_now"], start=1):
        print(
            f"{index:>2}. {trend['name']} | score={float(trend['score']):.1f} | "
            f"platforms={', '.join(trend['platforms'])} | observed={trend['observed_at']}"
        )


def _settings_summary(settings: Any, db_path: Path) -> dict[str, Any]:
    return {
        "app": settings.app.name,
        "environment": settings.app.environment,
        "paper_mode": settings.paper_mode,
        "cycle_interval_minutes": settings.app.cycle_interval_minutes,
        "trend_score_threshold": settings.app.trend_score_threshold,
        "database": db_path,
        "log_file": settings.app.log_file,
        "monitors": {
            "google_trends": settings.monitors.google_trends.enabled,
            "reddit": settings.monitors.reddit.enabled,
            "twitter": settings.monitors.twitter.enabled,
            "tiktok": settings.monitors.tiktok.enabled,
        },
        "telegram_enabled": settings.telegram.enabled,
        "domains_enabled": settings.domains.enabled,
    }


if __name__ == "__main__":
    raise SystemExit(main())


