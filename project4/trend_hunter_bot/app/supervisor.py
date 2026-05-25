from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from app.botmaster_status import write_status


BOT_ID = os.getenv("BOT_ID", "trend")
BOT_NAME = os.getenv("BOT_NAME", "Trend Hunter")
COMMAND = [sys.executable, "-m", "app.main", "scheduler"]

logger = logging.getLogger("supervisor")
stop_event = threading.Event()
active_process: subprocess.Popen[bytes] | None = None


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _heartbeat_seconds() -> int:
    return max(15, _env_int("BOTMASTER_HEARTBEAT_SECONDS", 30))


def _stop_timeout_seconds() -> int:
    return max(5, _env_int("BOT_SUPERVISOR_STOP_TIMEOUT_SECONDS", 20))


def _write_status(
    status: str,
    restart_count: int,
    *,
    pid: int | None = None,
    error: str | None = None,
) -> None:
    write_status(
        BOT_ID,
        BOT_NAME,
        status,
        {
            "supervisor": "app.supervisor",
            "pid": pid,
            "restart_count": restart_count,
            "heartbeat_seconds": _heartbeat_seconds(),
        },
        error=error,
    )


def _request_stop(signum: int, _frame: object) -> None:
    logger.info("Received signal %s. Stopping supervised bot.", signum)
    stop_event.set()
    if active_process and active_process.poll() is None:
        active_process.terminate()


def _install_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
    except ValueError:
        logger.debug("Signal handlers can only be installed from the main thread")


def _wait_for_process(process: subprocess.Popen[bytes], restart_count: int) -> int | None:
    next_heartbeat = time.monotonic() + _heartbeat_seconds()
    while process.poll() is None and not stop_event.is_set():
        now = time.monotonic()
        if now >= next_heartbeat:
            _write_status("RUNNING", restart_count, pid=process.pid)
            next_heartbeat = now + _heartbeat_seconds()
        time.sleep(1)
    return process.poll()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_stop_timeout_seconds())
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _sleep_before_restart(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def main() -> int:
    global active_process

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [supervisor] %(message)s")
    _install_signal_handlers()
    restart_count = 0

    while not stop_event.is_set():
        try:
            process = subprocess.Popen(COMMAND)
        except Exception as exc:  # noqa: BLE001
            restart_count += 1
            logger.exception("Could not start %s: %s", BOT_NAME, exc)
            _write_status("ERROR", restart_count, error=f"Could not start bot: {exc}")
            _sleep_before_restart(min(60, 5 * restart_count))
            continue
        active_process = process
        _write_status("RUNNING", restart_count, pid=process.pid)
        logger.info("Started %s pid=%s restart_count=%s", BOT_NAME, process.pid, restart_count)

        exit_code = _wait_for_process(process, restart_count)
        if stop_event.is_set():
            _stop_process(process)
            break

        restart_count += 1
        error = f"Bot process exited with code {exit_code}; restarting"
        logger.error("%s", error)
        _write_status("ERROR", restart_count, error=error)
        _sleep_before_restart(min(60, 5 * restart_count))

    if active_process:
        _stop_process(active_process)
    _write_status("STOPPED", restart_count)
    logger.info("%s supervisor stopped", BOT_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
