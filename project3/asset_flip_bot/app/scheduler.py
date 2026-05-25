from __future__ import annotations

import os
import signal
import threading
import time
from datetime import datetime, timezone

from app.botmaster_status import write_status
from app.config.settings import AppSettings
from app.services.asset_manager import AssetManager
from app.utils.logger import get_logger


def _heartbeat_seconds() -> int:
    value = os.getenv("BOTMASTER_HEARTBEAT_SECONDS", "30")
    try:
        return max(15, int(value))
    except ValueError:
        return 30


class BotScheduler:
    def __init__(self, settings: AppSettings, manager: AssetManager | None = None) -> None:
        self.settings = settings
        self.manager = manager or AssetManager(settings)
        self.stop_event = threading.Event()
        self.logger = get_logger("scheduler")

    def run_forever(self) -> None:
        self._install_signal_handlers()
        interval_seconds = max(60, self.settings.scan_interval_minutes * 60)
        self.logger.info(
            "Asset Flip Bot started. Interval=%s minutes paper_mode=%s",
            self.settings.scan_interval_minutes,
            self.settings.paper_mode,
        )
        write_status(
            "asset",
            "Asset Flip",
            "RUNNING",
            {
                "paper_mode": self.settings.paper_mode,
                "scan_interval_minutes": self.settings.scan_interval_minutes,
            },
        )
        while not self.stop_event.is_set():
            started = datetime.now(timezone.utc)
            try:
                summary = self.manager.scan_once()
                write_status(
                    "asset",
                    "Asset Flip",
                    "RUNNING",
                    {
                        "assets_scanned": summary.assets_monitored,
                        "opportunities_found": summary.opportunities_found,
                        "total_potential_profit": summary.total_potential_profit,
                        "alerts_sent": summary.alerted,
                    },
                )
            except Exception as exc:
                self.logger.exception("Scheduled scan failed: %s", exc)
                write_status("asset", "Asset Flip", "ERROR", error=str(exc))

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            sleep_for = max(0.0, interval_seconds - elapsed)
            self.logger.info("Next scan in %.0f seconds", sleep_for)
            self._wait_with_heartbeat(sleep_for)
        write_status("asset", "Asset Flip", "STOPPED")
        self.logger.info("Asset Flip Bot stopped")

    def stop(self) -> None:
        self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            self.logger.info("Received signal %s. Stopping after current scan.", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except ValueError:
            self.logger.debug("Signal handlers can only be installed from the main thread")

    def _wait_with_heartbeat(self, sleep_for: float) -> None:
        deadline = time.monotonic() + sleep_for
        heartbeat_seconds = _heartbeat_seconds()
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self.stop_event.wait(min(heartbeat_seconds, remaining)):
                return
            write_status(
                "asset",
                "Asset Flip",
                "RUNNING",
                {
                    "paper_mode": self.settings.paper_mode,
                    "scan_interval_minutes": self.settings.scan_interval_minutes,
                    "heartbeat_seconds": heartbeat_seconds,
                },
            )



