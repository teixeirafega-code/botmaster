from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.botmaster_status import write_status
from app.core.context import flow_context
from app.observability.health import HealthServer
from app.observability.metrics import runtime_status
from app.services.domain_manager import DomainManager

logger = logging.getLogger(__name__)


async def start_scheduler(manager: DomainManager, interval_minutes: int, timezone: str) -> None:
    async def send_daily_report() -> None:
        await manager.notifier.send_daily_report(await manager.load_state())

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
                },
            )
            return monitored
        except Exception as exc:
            write_status("domain", "Domain Hunter", "ERROR", error=str(exc))
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

    await manager.notifier.send_startup_alert()
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
        await manager.notifier.send_message("Domain Hunter Bot shutdown complete", disable_notification=True)
