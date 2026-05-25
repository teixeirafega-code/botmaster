from __future__ import annotations

import os
import signal
import threading
import time

from app.botmaster_status import write_status
from app.config.settings import Settings
from app.services.trend_manager import TrendManager
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _heartbeat_seconds() -> int:
    value = os.getenv("BOTMASTER_HEARTBEAT_SECONDS", "30")
    try:
        return max(15, int(value))
    except ValueError:
        return 30


class TrendScheduler:
    def __init__(self, settings: Settings, manager: TrendManager | None = None) -> None:
        self.settings = settings
        self.manager = manager or TrendManager(settings)
        self.stop_event = threading.Event()

    def run_forever(self) -> None:
        self._install_signal_handlers()
        interval_seconds = self.settings.app.cycle_interval_minutes * 60
        logger.info("Scheduler started; cycle interval is %d seconds", interval_seconds)
        write_status(
            "trend",
            "Trend Hunter",
            "RUNNING",
            {"cycle_interval_minutes": self.settings.app.cycle_interval_minutes},
        )

        while not self.stop_event.is_set():
            cycle_started = time.monotonic()
            try:
                summary = self.manager.run_cycle()
                snapshot = self.manager.dashboard_snapshot(top_limit=5)
                metrics = summary.to_dict()
                metrics.update(
                    {
                        "trends_detected_today": snapshot.get("trends_detected_today", 0),
                        "domains_registered_today": snapshot.get("domains_registered_today", 0),
                        "top_topics": snapshot.get("top_trending_now", []),
                    }
                )
                write_status("trend", "Trend Hunter", "RUNNING", metrics)
            except Exception as exc:
                logger.exception("Scheduled trend cycle failed: %s", exc)
                write_status("trend", "Trend Hunter", "ERROR", error=str(exc))
            elapsed = time.monotonic() - cycle_started
            wait_seconds = max(interval_seconds - elapsed, 1.0)
            logger.info("Next collection cycle starts in %.0f seconds", wait_seconds)
            self._wait_with_heartbeat(wait_seconds)

        write_status("trend", "Trend Hunter", "STOPPED")
        logger.info("Scheduler stopped")

    def stop(self) -> None:
        self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except ValueError:
            logger.debug("Signal handlers can only be installed from the main thread")

    def _handle_signal(self, signum: int, _frame: object) -> None:
        logger.info("Received signal %s; stopping scheduler", signum)
        self.stop()

    def _wait_with_heartbeat(self, wait_seconds: float) -> None:
        deadline = time.monotonic() + wait_seconds
        heartbeat_seconds = _heartbeat_seconds()
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self.stop_event.wait(min(heartbeat_seconds, remaining)):
                return
            write_status(
                "trend",
                "Trend Hunter",
                "RUNNING",
                {
                    "cycle_interval_minutes": self.settings.app.cycle_interval_minutes,
                    "heartbeat_seconds": heartbeat_seconds,
                },
            )


