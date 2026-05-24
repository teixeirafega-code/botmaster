from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_status(
    bot_id: str,
    bot_name: str,
    status: str,
    metrics: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "bot_id": bot_id,
        "bot_name": bot_name,
        "status": status.upper(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics or {},
        "error": error,
    }
    _write_sqlite(payload)
    _post_dashboard(payload)


def _db_path() -> Path:
    raw = os.getenv("BOTMASTER_SHARED_DB")
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / "data" / "botmaster_status.sqlite"


def _write_sqlite(payload: dict[str, Any]) -> None:
    try:
        db_path = _db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path, timeout=10) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
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
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    bot_name=excluded.bot_name,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    metrics_json=excluded.metrics_json,
                    error=excluded.error
                """,
                (
                    payload["bot_id"],
                    payload["bot_name"],
                    payload["status"],
                    payload["updated_at"],
                    json.dumps(payload["metrics"], ensure_ascii=True, default=str),
                    payload["error"],
                ),
            )
    except Exception:
        return


def _dashboard_endpoint() -> str | None:
    endpoint = os.getenv("BOTMASTER_STATUS_ENDPOINT")
    if endpoint:
        return endpoint.rstrip("/")
    hostport = os.getenv("BOTMASTER_STATUS_HOSTPORT")
    if hostport:
        hostport = hostport.strip().rstrip("/")
        if hostport.startswith("http://") or hostport.startswith("https://"):
            return f"{hostport}/api/ingest"
        return f"http://{hostport}/api/ingest"
    public_url = os.getenv("DASHBOARD_PUBLIC_URL")
    if public_url:
        return f"{public_url.rstrip('/')}/api/ingest"
    return None


def _post_dashboard(payload: dict[str, Any]) -> None:
    endpoint = _dashboard_endpoint()
    token = os.getenv("BOTMASTER_STATUS_TOKEN")
    if not endpoint or not token:
        return
    body = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-BotMaster-Token": token,
        },
    )
    try:
        with urlopen(request, timeout=5):
            return
    except (OSError, URLError):
        return
