from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import runpy
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BOTMASTER_ROOT = Path(__file__).resolve().parent
DEFAULT_DASHBOARD_URL = "https://botmaster-l17d.onrender.com"
HEARTBEAT_SECONDS = max(15, int(os.getenv("BOTMASTER_HEARTBEAT_SECONDS", "30")))


@dataclass(frozen=True)
class BotSpec:
    bot_id: str
    name: str
    relative_dir: str

    @property
    def root(self) -> Path:
        return BOTMASTER_ROOT / self.relative_dir


BOTS = (
    BotSpec("yield", "Yield Optimizer", "project1/yield_optimizer_bot"),
    BotSpec("domain", "Domain Hunter", "project2/projeto2"),
    BotSpec("asset", "Asset Flip", "project3/asset_flip_bot"),
    BotSpec("trend", "Trend Hunter", "project4/trend_hunter_bot"),
)


def shared_db_path() -> Path:
    raw = os.getenv("BOTMASTER_SHARED_DB")
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else BOTMASTER_ROOT / path
    render_disk = Path("/var/data")
    if render_disk.exists():
        return render_disk / "botmaster_status.sqlite"
    return BOTMASTER_ROOT / "data" / "botmaster_status.sqlite"


def write_status(
    spec: BotSpec,
    status: str,
    metrics: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "bot_id": spec.bot_id,
        "bot_name": spec.name,
        "status": status.upper(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metrics_json": json.dumps(metrics or {}, ensure_ascii=True, default=str),
        "error": error,
    }
    try:
        db_path = shared_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path, timeout=10) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_status (
                    bot_id TEXT PRIMARY KEY,
                    bot_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO bot_status (bot_id, bot_name, status, updated_at, metrics_json, error)
                VALUES (:bot_id, :bot_name, :status, :updated_at, :metrics_json, :error)
                ON CONFLICT(bot_id) DO UPDATE SET
                    bot_name=excluded.bot_name,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    metrics_json=excluded.metrics_json,
                    error=excluded.error
                """,
                payload,
            )
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not write %s status: %s", spec.name, exc)


def run_bot(spec: BotSpec) -> None:
    bot_dir = spec.root
    if not bot_dir.exists():
        raise FileNotFoundError(f"Bot directory not found: {bot_dir}")

    os.chdir(bot_dir)
    os.environ.setdefault("BOT_ID", spec.bot_id)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("BOTMASTER_DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
    os.environ.setdefault("BOTMASTER_SHARED_DB", str(shared_db_path()))
    sys.path.insert(0, str(bot_dir))
    sys.argv = ["python -m app.main", "scheduler"]

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [{spec.name}] %(message)s",
    )
    logging.info("Starting %s from %s", spec.name, bot_dir)
    runpy.run_module("app.main", run_name="__main__")


def start_process(context: mp.context.BaseContext, spec: BotSpec, restart_count: int) -> mp.Process:
    process = context.Process(target=run_bot, args=(spec,), name=spec.name)
    process.start()
    write_status(
        spec,
        "RUNNING",
        {
            "supervisor": "runner.py",
            "pid": process.pid,
            "restart_count": restart_count,
            "root": str(spec.root),
        },
    )
    logging.info("Started %s pid=%s restart_count=%s", spec.name, process.pid, restart_count)
    return process


def stop_processes(processes: dict[str, tuple[BotSpec, mp.Process]], timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    for spec, process in processes.values():
        if process.is_alive():
            process.terminate()
        write_status(spec, "STOPPED", {"supervisor": "runner.py", "pid": process.pid})
    for _spec, process in processes.values():
        remaining = max(0.0, deadline - time.monotonic())
        process.join(timeout=remaining)
    for _spec, process in processes.values():
        if process.is_alive():
            process.kill()
    for _spec, process in processes.values():
        process.join(timeout=2)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [runner] %(message)s")
    os.environ.setdefault("BOTMASTER_DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
    os.environ.setdefault("BOTMASTER_SHARED_DB", str(shared_db_path()))
    context = mp.get_context("spawn")
    restart_counts = {spec.bot_id: 0 for spec in BOTS}
    processes = {spec.bot_id: (spec, start_process(context, spec, 0)) for spec in BOTS}
    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    stopping = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        logging.info("Received signal %s. Stopping all bots.", signum)
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        while not stopping:
            now = time.monotonic()
            if now >= next_heartbeat:
                for spec, process in processes.values():
                    if process.is_alive():
                        write_status(
                            spec,
                            "RUNNING",
                            {
                                "supervisor": "runner.py",
                                "pid": process.pid,
                                "restart_count": restart_counts[spec.bot_id],
                                "heartbeat_seconds": HEARTBEAT_SECONDS,
                            },
                        )
                next_heartbeat = now + HEARTBEAT_SECONDS

            for bot_id, (spec, process) in list(processes.items()):
                if process.exitcode is None:
                    continue
                exit_code = process.exitcode
                process.join(timeout=1)
                restart_counts[bot_id] += 1
                logging.error("%s exited with code %s. Restarting bot only.", spec.name, exit_code)
                write_status(
                    spec,
                    "ERROR",
                    {
                        "supervisor": "runner.py",
                        "restart_count": restart_counts[bot_id],
                    },
                    error=f"Process exited with code {exit_code}; restarting",
                )
                time.sleep(min(60, 5 * restart_counts[bot_id]))
                processes[bot_id] = (spec, start_process(context, spec, restart_counts[bot_id]))
            time.sleep(2)
    finally:
        stop_processes(processes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
