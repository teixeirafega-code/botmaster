from __future__ import annotations

import asyncio
import time
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger


class SchedulerFactory:
    @staticmethod
    def create(
        monitor_interval_seconds: int,
        rebalance_interval_seconds: int,
        healthcheck_interval_seconds: int,
        heartbeat_interval_seconds: int,
        timezone: str,
        monitor_job: Callable[[], asyncio.Future],
        rebalance_job: Callable[[], asyncio.Future],
        healthcheck_job: Callable[[], asyncio.Future],
        heartbeat_job: Callable[[], asyncio.Future],
    ) -> AsyncIOScheduler:
        scheduler = AsyncIOScheduler(timezone=timezone)

        scheduler.add_job(monitor_job, IntervalTrigger(seconds=monitor_interval_seconds), id="monitor_apy", replace_existing=True)
        scheduler.add_job(rebalance_job, IntervalTrigger(seconds=rebalance_interval_seconds), id="rebalance", replace_existing=True)
        scheduler.add_job(healthcheck_job, IntervalTrigger(seconds=healthcheck_interval_seconds), id="healthcheck", replace_existing=True)
        scheduler.add_job(heartbeat_job, IntervalTrigger(seconds=heartbeat_interval_seconds), id="heartbeat", replace_existing=True)

        return scheduler

