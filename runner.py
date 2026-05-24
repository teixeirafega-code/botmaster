from __future__ import annotations

import logging
import multiprocessing as mp
import os
import runpy
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BOTMASTER_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BotSpec:
    bot_id: str
    name: str
    relative_dir: str


BOTS = (
    BotSpec("yield", "Yield Optimizer", "project1/yield_optimizer_bot"),
    BotSpec("domain", "Domain Hunter", "project2/projeto2"),
    BotSpec("asset", "Asset Flip", "project3/asset_flip_bot"),
    BotSpec("trend", "Trend Hunter", "project4/trend_hunter_bot"),
)


def run_bot(spec: BotSpec) -> None:
    bot_dir = BOTMASTER_ROOT / spec.relative_dir
    if not bot_dir.exists():
        raise FileNotFoundError(f"Bot directory not found: {bot_dir}")

    os.chdir(bot_dir)
    os.environ.setdefault("BOT_ID", spec.bot_id)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.path.insert(0, str(bot_dir))
    sys.argv = ["python -m app.main", "scheduler"]

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [{spec.name}] %(message)s",
    )
    logging.info("Starting %s from %s", spec.name, bot_dir)
    runpy.run_module("app.main", run_name="__main__")


def start_processes(context: mp.context.BaseContext) -> list[mp.Process]:
    processes: list[mp.Process] = []
    for spec in BOTS:
        process = context.Process(target=run_bot, args=(spec,), name=spec.name)
        process.start()
        processes.append(process)
    return processes


def stop_processes(processes: list[mp.Process], timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        process.join(timeout=remaining)
    for process in processes:
        if process.is_alive():
            process.kill()
    for process in processes:
        process.join(timeout=2)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [runner] %(message)s")
    context = mp.get_context("spawn")
    processes = start_processes(context)
    stopping = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        logging.info("Received signal %s. Stopping all bots.", signum)
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        while not stopping:
            for process in processes:
                if process.exitcode is not None:
                    logging.error("%s exited with code %s. Restarting the full worker.", process.name, process.exitcode)
                    stopping = True
                    break
            time.sleep(2)
    finally:
        stop_processes(processes)

    failed = [process for process in processes if process.exitcode not in (0, None, -signal.SIGTERM)]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
