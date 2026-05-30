from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections import Counter
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.botmaster_status import write_status
from app.core.context import flow_context
from app.observability.health import HealthServer
from app.observability.metrics import runtime_status
from app.services.domain_manager import DomainManager

logger = logging.getLogger(__name__)


async def start_scheduler(manager: DomainManager, interval_minutes: int, timezone: str) -> None:
    async def send_daily_report() -> None:
        await manager.reprice_stale_listings()
        await manager.notifier.send_domain_daily_summary(await _daily_domain_summary(manager))

    async def run_scan_job() -> list[object]:
        try:
            monitored = await manager.run_cycle()
            registered = [
                item.name
                for item in monitored
                if getattr(item.status, "value", item.status) in {"registered", "listed", "sold"}
            ]
            opportunities = sum(
                1
                for item in monitored
                if getattr(item, "score", 0) >= manager.settings.scoring.registration_threshold
            )
            write_status(
                "domain",
                "Domain Hunter",
                "RUNNING",
                {
                    "domains_scanned_today": runtime_status.domains_scanned,
                    "opportunities_found": opportunities,
                    "domains_registered": runtime_status.domains_registered,
                    "last_domain_registered": registered[-1] if registered else None,
                    "paper_mode": manager.settings.paper_mode,
                    "safe_mode": manager.settings.safe_mode,
                    "dry_run_purchases": manager.settings.dry_run_purchases,
                    "policy_counters": manager.acquisition_policy.counters_snapshot(),
                },
            )
            return monitored
        except Exception as exc:
            write_status("domain", "Domain Hunter", "ERROR", error=str(exc))
            await manager.notifier.send_error("Scheduled scan job failed", exc, critical=True)
            raise

    scheduler = AsyncIOScheduler(
        timezone=timezone,
        job_defaults={
            "coalesce": True,
            "max_instances": manager.settings.scheduler.max_instances,
            "misfire_grace_time": 60,
        },
    )
    health_server = HealthServer(
        manager.repository,
        manager.settings.observability.health_host,
        manager.settings.observability.health_port,
    )
    scheduler.add_job(
        run_scan_job,
        "interval",
        minutes=interval_minutes,
        next_run_time=None,
        id="domain_scan",
        replace_existing=True,
    )
    scheduler.add_job(send_daily_report, "cron", hour=9, minute=0)
    scheduler.start()
    await health_server.start()
    runtime_status.scheduler_running = True
    write_status(
        "domain",
        "Domain Hunter",
        "RUNNING",
        {
            "domains_scanned_today": runtime_status.domains_scanned,
            "domains_registered": runtime_status.domains_registered,
            "paper_mode": manager.settings.paper_mode,
            "safe_mode": manager.settings.safe_mode,
            "dry_run_purchases": manager.settings.dry_run_purchases,
            "policy_counters": manager.acquisition_policy.counters_snapshot(),
        },
    )
    logger.info("Scheduler started: every %s minutes", interval_minutes)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await manager.notifier.send_startup_health_check()
    try:
        with flow_context(execution_mode="paper" if manager.settings.paper_mode else "live"):
            await run_scan_job()
    except Exception as exc:
        logger.exception("Initial scheduled cycle failed")
        await manager.notifier.send_error("Initial scheduled cycle failed", exc, critical=True)
    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=True)
        await health_server.stop()
        runtime_status.scheduler_running = False
        write_status("domain", "Domain Hunter", "STOPPED")


async def _daily_domain_summary(manager: DomainManager) -> dict[str, int]:
    counters = manager.acquisition_policy.counters_snapshot()
    approvals = _read_json(manager.settings.pending_approvals_file, {})
    attempts = _read_json(manager.settings.purchase_attempts_file, [])
    portfolio = await manager.load_state()
    pending_approvals = 0
    manual_approvals = 0
    if isinstance(approvals, dict):
        for value in approvals.values():
            if not isinstance(value, dict):
                continue
            if value.get("approved") is True:
                manual_approvals += 1
            else:
                pending_approvals += 1
    dry_run_domains = {
        str(item.get("domain"))
        for item in attempts
        if isinstance(item, dict) and item.get("blocked_by_dry_run") is True and item.get("domain")
    } if isinstance(attempts, list) else set()
    real_purchases = sum(
        1
        for domain in portfolio
        if getattr(domain.status, "value", domain.status) in {"registered", "listed", "sold"}
        and domain.acquisition_cost > 0
        and domain.name not in dry_run_domains
    )
    reason_counts = Counter(
        {
            key.split(":", 1)[1]: value
            for key, value in counters.items()
            if key.startswith("reason:")
        }
    )
    return {
        "domains_scanned": runtime_status.domains_scanned,
        "opportunities_found": counters.get("decisions_total", 0),
        "pending_approvals": pending_approvals,
        "trademark_blocks": _reason_total(reason_counts, "trademark"),
        "liquidity_blocks": _reason_total(reason_counts, "liquidity"),
        "budget_blocks": _reason_total(reason_counts, "budget"),
        "manual_approvals": manual_approvals,
        "real_purchases": real_purchases,
    }


def _read_json(path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _reason_total(reason_counts: Counter[str], category: str) -> int:
    total = 0
    for reason, count in reason_counts.items():
        if category == "trademark" and "trademark" in reason:
            total += count
        elif category == "liquidity" and ("liquidity" in reason or "expected_value" in reason):
            total += count
        elif category == "budget" and any(token in reason for token in ("spend", "budget", "price_above", "max_buys", "max_portfolio")):
            total += count
    return total
