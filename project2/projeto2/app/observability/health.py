from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from app.core.resilience import resilience_registry
from app.db.postgres import DomainRepository
from app.observability.metrics import prometheus_metrics, runtime_status

logger = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, repository: DomainRepository, host: str, port: int) -> None:
        self.repository = repository
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_get("/metrics", self.metrics)
        app.router.add_get("/status", self.status)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("health_server_started", extra={"event_name": "health_server_started"})

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def health(self, _: web.Request) -> web.Response:
        db_ok = await self.repository.healthcheck()
        status = 200 if db_ok else 503
        return web.json_response({"ok": db_ok, "db": db_ok}, status=status)

    async def metrics(self, _: web.Request) -> web.Response:
        return web.Response(text=prometheus_metrics(), content_type="text/plain")

    async def status(self, _: web.Request) -> web.Response:
        payload: dict[str, Any] = {
            "scheduler_running": runtime_status.scheduler_running,
            "last_successful_scan": runtime_status.last_successful_scan.isoformat() if runtime_status.last_successful_scan else None,
            "api_provider_status": runtime_status.provider_status,
            "db_connectivity": await self.repository.healthcheck(),
            "queue_depth": runtime_status.queue_depth,
            "circuit_breakers": resilience_registry.states(),
            "api_retries": runtime_status.api_retries,
            "retry_budget_exhaustions": runtime_status.retry_budget_exhaustions,
            "event_published": runtime_status.event_published,
            "event_dead_letters": runtime_status.event_dead_letters,
            "async_task_count": len(asyncio.all_tasks()),
            "memory_kb": self._memory_kb(),
        }
        return web.json_response(payload)

    def _memory_kb(self) -> int:
        return 0
