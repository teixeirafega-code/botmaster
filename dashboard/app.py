from __future__ import annotations

import csv
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for


BASE_DIR = Path(__file__).resolve().parent
BOTMASTER_ROOT = Path(os.getenv("BOTMASTER_ROOT", BASE_DIR.parent)).resolve()
load_dotenv(BASE_DIR / ".env")

PASSWORD = os.getenv("DASHBOARD_PASSWORD")
SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not PASSWORD:
    raise RuntimeError("DASHBOARD_PASSWORD must be set in .env or environment variables")
if not SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY must be set in .env or environment variables")


app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


BOT_CONFIG = {
    "yield": {
        "label": "Yield Optimizer",
        "roots": [BOTMASTER_ROOT / "project1" / "yield_optimizer_bot"],
        "logs": ["logs/bot.log"],
        "states": ["app/data/state.json", "app/data/paper_state.json"],
    },
    "domain": {
        "label": "Domain Hunter",
        "roots": [
            BOTMASTER_ROOT / "project2" / "projeto2",
            BOTMASTER_ROOT / "projeto2",
        ],
        "logs": ["logs/domain_hunter_bot.log"],
        "states": ["state.json", "data/domains.json"],
    },
    "asset": {
        "label": "Asset Flip",
        "roots": [BOTMASTER_ROOT / "project3" / "asset_flip_bot"],
        "logs": ["logs/asset_flip_bot.log"],
        "states": ["state.json", "data/profit_stats.json", "data/assets_state.json"],
    },
    "trend": {
        "label": "Trend Hunter",
        "roots": [BOTMASTER_ROOT / "project4" / "trend_hunter_bot"],
        "logs": ["logs/trend_hunter.log"],
        "states": ["state.json", "trend_hunter.db"],
    },
}


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, PASSWORD):
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Senha invalida."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    if not _is_authenticated():
        return redirect(url_for("login"))
    snapshot = build_snapshot()
    return render_template("dashboard.html", snapshot=snapshot)


@app.get("/api/status")
def api_status():
    return jsonify(build_snapshot())


@app.post("/api/ingest")
def api_ingest():
    token = os.getenv("BOTMASTER_STATUS_TOKEN")
    supplied = request.headers.get("X-BotMaster-Token", "")
    if not token or not hmac.compare_digest(supplied, token):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid json payload"}), 400
    try:
        _write_shared_status(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "bot_id": payload.get("bot_id")})


def _is_authenticated() -> bool:
    return session.get("authenticated") is True


def build_snapshot() -> dict[str, Any]:
    process_lines = _process_command_lines()
    now = _now()
    cards = [
        _yield_card(process_lines, now),
        _domain_card(process_lines, now),
        _asset_card(process_lines, now),
        _trend_card(process_lines, now),
    ]
    cards = _merge_shared_statuses(cards, _read_shared_statuses(), now)
    running = sum(1 for card in cards if card["status"] == "RUNNING")
    return {
        "generated_at": now.isoformat(),
        "generated_label": _format_dt(now),
        "running_count": running,
        "stopped_count": len(cards) - running,
        "cards": cards,
    }


def _yield_card(process_lines: list[str], now: datetime) -> dict[str, Any]:
    root = _bot_root("yield")
    state = _read_json(root / "app/data/state.json")
    log_path = root / "logs/bot.log"
    logs = _tail_lines(log_path, 900)

    current_apys = _latest_yield_apys(state, logs)
    best_protocol = max(
        ((name, value) for name, value in current_apys.items() if value is not None),
        key=lambda item: item[1],
        default=(None, None),
    )
    simulated_profit = _latest_number_from_logs(
        logs,
        ["expected_profit_usd", "pnl_usd", "accrued_simulated_yield_usd"],
    )

    last_update = _latest_timestamp([root / "app/data/state.json", log_path], logs)
    return {
        "id": "yield",
        "name": "Yield Optimizer",
        "accent": "green",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "metrics": [
            {"label": "Best protocol", "value": _title(best_protocol[0]) if best_protocol[0] else "No APY data"},
            {"label": "Simulated profit", "value": _format_money(simulated_profit)},
        ],
        "apys": [
            {"protocol": "Aave", "value": _format_percent(current_apys.get("aave"))},
            {"protocol": "Compound", "value": _format_percent(current_apys.get("compound"))},
            {"protocol": "Curve", "value": _format_percent(current_apys.get("curve"))},
            {"protocol": "Beefy", "value": _format_percent(current_apys.get("beefy"))},
        ],
        "details": [],
    }


def _domain_card(process_lines: list[str], now: datetime) -> dict[str, Any]:
    root = _bot_root("domain")
    log_path = root / "logs/domain_hunter_bot.log"
    logs = _json_log_tail(log_path, 5000)
    domains_state = _read_json(root / "data/domains.json", default=[])
    today = now.astimezone().date()

    scanned_today = 0
    opportunities_today = 0
    last_registered = None
    last_registered_time = None

    for row in logs:
        event = str(row.get("event_name", "")).upper()
        timestamp = _parse_dt(row.get("timestamp"))
        domain = row.get("domain")
        score = _safe_float(row.get("score"))
        if timestamp and timestamp.astimezone().date() == today and event == "DOMAIN_SCORED":
            scanned_today += 1
            if score is not None and score >= 70:
                opportunities_today += 1
        if domain and ("REGISTER" in event or "PURCHASE" in event):
            if last_registered_time is None or (timestamp and timestamp > last_registered_time):
                last_registered = str(domain)
                last_registered_time = timestamp

    if scanned_today == 0 and isinstance(domains_state, list):
        scanned_today = len(domains_state)
        opportunities_today = sum(1 for item in domains_state if _safe_float(item.get("score")) and item.get("score") >= 70)

    last_update = _latest_timestamp([log_path, root / "data/domains.json"], [])
    return {
        "id": "domain",
        "name": "Domain Hunter",
        "accent": "blue",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "metrics": [
            {"label": "Domains scanned today", "value": _format_int(scanned_today)},
            {"label": "Opportunities found", "value": _format_int(opportunities_today)},
            {"label": "Last domain registered", "value": last_registered or "No registration in logs"},
        ],
        "details": [],
    }


def _asset_card(process_lines: list[str], now: datetime) -> dict[str, Any]:
    root = _bot_root("asset")
    stats_path = root / "data/profit_stats.json"
    state_path = root / "data/assets_state.json"
    stats = _read_json(stats_path)
    state = _read_json(state_path)
    log_path = root / "logs/asset_flip_bot.log"

    assets_scanned = _first_number(stats, "assets_monitored", "assets_scanned")
    if assets_scanned is None and isinstance(state.get("latest_listings"), list):
        assets_scanned = len(state["latest_listings"])

    opportunities = _first_number(stats, "opportunities_found")
    if opportunities is None and isinstance(stats.get("opportunities"), list):
        opportunities = len(stats["opportunities"])

    total_profit = _first_number(stats, "total_potential_profit")
    if total_profit is None and isinstance(stats.get("opportunities"), list):
        total_profit = sum(
            _safe_float(item.get("valuation", {}).get("profit_potential")) or 0
            for item in stats["opportunities"]
        )

    top_details = []
    opportunities_list = stats.get("opportunities") if isinstance(stats.get("opportunities"), list) else []
    for item in sorted(opportunities_list, key=lambda row: _safe_float(row.get("opportunity_score")) or 0, reverse=True)[:3]:
        listing = item.get("listing", {}) if isinstance(item, dict) else {}
        valuation = item.get("valuation", {}) if isinstance(item, dict) else {}
        top_details.append(
            {
                "label": str(listing.get("name") or "Unnamed asset")[:86],
                "value": _format_money(_safe_float(valuation.get("profit_potential"))),
            }
        )

    last_update = _latest_timestamp([stats_path, state_path, log_path], [])
    return {
        "id": "asset",
        "name": "Asset Flip",
        "accent": "amber",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "metrics": [
            {"label": "Assets scanned", "value": _format_int(assets_scanned)},
            {"label": "Opportunities found", "value": _format_int(opportunities)},
            {"label": "Total potential profit", "value": _format_money(total_profit)},
        ],
        "details": top_details,
    }


def _trend_card(process_lines: list[str], now: datetime) -> dict[str, Any]:
    root = _bot_root("trend")
    db_path = root / "trend_hunter.db"
    log_path = root / "logs/trend_hunter.log"
    trends_today, top_topics = _read_trend_db(db_path, now)
    if trends_today is None:
        trends_today, top_topics = _read_trend_logs(log_path, now)

    last_update = _latest_timestamp([db_path, log_path], [])
    return {
        "id": "trend",
        "name": "Trend Hunter",
        "accent": "purple",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "metrics": [
            {"label": "Trends detected today", "value": _format_int(trends_today)},
        ],
        "details": [
            {"label": topic.get("name", "Unknown trend"), "value": _format_score(topic.get("score"))}
            for topic in top_topics[:5]
        ],
    }


def _bot_root(bot_id: str) -> Path:
    for root in BOT_CONFIG[bot_id]["roots"]:
        if root.exists():
            return root
    return BOT_CONFIG[bot_id]["roots"][0]


def _bot_status(root: Path, process_lines: list[str], last_update: datetime | None, now: datetime) -> str:
    root_text = str(root).lower()
    if any(root_text in line.lower() for line in process_lines):
        return "RUNNING"
    if last_update and (now - last_update.astimezone(timezone.utc)) <= timedelta(minutes=5):
        return "RUNNING"
    return "STOPPED"


def _process_command_lines() -> list[str]:
    try:
        if os.name == "nt":
            command = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine",
            ]
        else:
            command = ["ps", "-eo", "command="]
        result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=False)
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []
    return []


def _latest_yield_apys(state: dict[str, Any], logs: list[str]) -> dict[str, float | None]:
    result = {"aave": None, "compound": None, "curve": None, "beefy": None}
    latest_ts = {name: -1.0 for name in result}

    history = state.get("apy_history", {})
    if isinstance(history, dict):
        for records in history.values():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                protocol = str(record.get("protocol", "")).lower()
                value = _safe_float(record.get("apy"))
                ts = _safe_float(record.get("ts")) or 0
                if protocol in result and value is not None and ts >= latest_ts[protocol]:
                    result[protocol] = value
                    latest_ts[protocol] = ts

    pattern = re.compile(r"protocol=([a-zA-Z0-9_-]+).*?(?:net_apy|raw_apy)=([0-9.]+)")
    for line in logs:
        match = pattern.search(line)
        if not match:
            continue
        protocol = match.group(1).lower()
        value = _safe_float(match.group(2))
        if protocol in result and value is not None:
            result[protocol] = value
    return result


def _read_trend_db(db_path: Path, now: datetime) -> tuple[int | None, list[dict[str, Any]]]:
    if not db_path.exists():
        return None, []
    start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    last_24h = now - timedelta(hours=24)
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            trends_today = connection.execute(
                "SELECT COUNT(DISTINCT normalized_name) FROM trends WHERE observed_at >= ?",
                (start.isoformat(),),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT name, score, platforms_json, observed_at
                FROM trends
                WHERE observed_at >= ?
                ORDER BY score DESC, observed_at DESC
                LIMIT 5
                """,
                (last_24h.isoformat(),),
            ).fetchall()
        top = [
            {
                "name": row["name"],
                "score": row["score"],
                "platforms": _json_loads(row["platforms_json"], []),
            }
            for row in rows
        ]
        return int(trends_today), top
    except sqlite3.Error:
        return None, []


def _read_trend_logs(log_path: Path, now: datetime) -> tuple[int, list[dict[str, Any]]]:
    logs = _tail_lines(log_path, 800)
    today = now.astimezone().date()
    count = 0
    top: list[dict[str, Any]] = []
    for line in logs:
        timestamp = _parse_dt(line[:23])
        if timestamp and timestamp.astimezone().date() == today and "trends_scored" in line:
            match = re.search(r"'trends_scored':\s*([0-9]+)", line)
            if match:
                count = max(count, int(match.group(1)))
        trend_match = re.search(r"Trend Hunter Alert.*?Trend:</b>\s*([^<]+).*?Score:</b>\s*([0-9.]+)", line)
        if trend_match:
            top.append({"name": trend_match.group(1), "score": _safe_float(trend_match.group(2))})
    return count, top[:5]


def _json_log_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    rows = []
    for line in _tail_lines(path, limit):
        item = _json_loads(line, None)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=limit)]
    except OSError:
        return []


def _read_json(path: Path, default: Any | None = None) -> Any:
    if default is None:
        default = {}
    if not path.exists() or not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _latest_timestamp(paths: list[Path], logs: list[str]) -> datetime | None:
    candidates: list[datetime] = []
    for path in paths:
        try:
            if path.exists():
                candidates.append(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        except OSError:
            pass
    for line in logs[-200:]:
        parsed = _parse_dt(line)
        if parsed:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def _latest_number_from_logs(logs: list[str], keys: list[str]) -> float | None:
    value = None
    pattern = re.compile(r"({})=([-+]?[0-9]*\.?[0-9]+)".format("|".join(re.escape(key) for key in keys)))
    for line in logs:
        for match in pattern.finditer(line):
            value = _safe_float(match.group(2))
    return value


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    json_match = re.search(r'"ts"\s*:\s*"([^"]+)"', text)
    if json_match:
        text = json_match.group(1)
    else:
        timestamp_match = re.search(r'"timestamp"\s*:\s*"([^"]+)"', text)
        if timestamp_match:
            text = timestamp_match.group(1)
        elif re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            text = text[:23]

    for parser in (
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S,%f"),
        lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser(text)
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _safe_float(data.get(key))
        if value is not None:
            return value
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_money(value: float | None) -> str:
    if value is None:
        return "$0.00"
    abs_value = abs(value)
    if abs_value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:,.2f}T"
    if abs_value >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if abs_value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    return f"${value:,.2f}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.2f}%"


def _format_int(value: float | int | None) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}"


def _format_score(value: Any) -> str:
    number = _safe_float(value)
    return "N/A" if number is None else f"{number:.1f}"


def _format_dt(value: datetime | None) -> str:
    if not value:
        return "No data"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _title(value: str | None) -> str:
    return value.replace("_", " ").replace("-", " ").title() if value else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)



def _shared_db_path() -> Path:
    raw = os.getenv("BOTMASTER_SHARED_DB")
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else BASE_DIR / path
    return BASE_DIR / "data" / "botmaster_status.sqlite"


def _ensure_shared_status_schema(connection: sqlite3.Connection) -> None:
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


def _write_shared_status(payload: dict[str, Any]) -> None:
    bot_id = str(payload.get("bot_id") or "").strip()
    bot_name = str(payload.get("bot_name") or bot_id).strip()
    status = str(payload.get("status") or "STOPPED").strip().upper()
    updated_at = str(payload.get("updated_at") or _now().isoformat())
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    error = payload.get("error")
    if bot_id not in BOT_CONFIG:
        raise ValueError("unknown bot_id")
    db_path = _shared_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        _ensure_shared_status_schema(connection)
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
            (bot_id, bot_name, status, updated_at, json.dumps(metrics, ensure_ascii=True, default=str), error),
        )


def _read_shared_statuses() -> dict[str, dict[str, Any]]:
    db_path = _shared_db_path()
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(db_path, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT bot_id, bot_name, status, updated_at, metrics_json, error FROM bot_status"
            ).fetchall()
    except sqlite3.Error:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[str(row["bot_id"])] = {
            "bot_name": row["bot_name"],
            "status": row["status"],
            "updated_at": row["updated_at"],
            "updated_at_dt": _parse_dt(row["updated_at"]),
            "metrics": _json_loads(row["metrics_json"], {}),
            "error": row["error"],
        }
    return result


def _merge_shared_statuses(
    cards: list[dict[str, Any]],
    shared: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    for card in cards:
        status = shared.get(str(card.get("id")))
        if not status:
            continue
        updated_at = status.get("updated_at_dt")
        is_fresh = bool(updated_at and (now - updated_at.astimezone(timezone.utc)) <= timedelta(minutes=10))
        card["status"] = str(status.get("status") or "STOPPED").upper() if is_fresh else "STOPPED"
        if updated_at:
            card["last_update"] = _format_dt(updated_at)
        metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
        _apply_shared_metrics(card, metrics)
        if status.get("error"):
            details = card.setdefault("details", [])
            details.insert(0, {"label": "Last error", "value": str(status["error"])[:140]})
    return cards


def _apply_shared_metrics(card: dict[str, Any], metrics: dict[str, Any]) -> None:
    bot_id = str(card.get("id"))
    if bot_id == "yield":
        apys = metrics.get("apys")
        if isinstance(apys, dict):
            for item in card.get("apys", []):
                key = str(item.get("protocol", "")).lower()
                item["value"] = _format_percent(_safe_float(apys.get(key)))
        if metrics.get("best_protocol"):
            _replace_metric(card, "Best protocol", _title(str(metrics.get("best_protocol"))))
        _replace_metric(card, "Simulated profit", _format_money(_safe_float(metrics.get("simulated_profit"))))
    elif bot_id == "domain":
        _replace_metric(card, "Domains scanned today", _format_int(_safe_float(metrics.get("domains_scanned_today"))))
        _replace_metric(card, "Opportunities found", _format_int(_safe_float(metrics.get("opportunities_found"))))
        if metrics.get("last_domain_registered"):
            _replace_metric(card, "Last domain registered", str(metrics.get("last_domain_registered")))
    elif bot_id == "asset":
        _replace_metric(card, "Assets scanned", _format_int(_safe_float(metrics.get("assets_scanned"))))
        _replace_metric(card, "Opportunities found", _format_int(_safe_float(metrics.get("opportunities_found"))))
        _replace_metric(card, "Total potential profit", _format_money(_safe_float(metrics.get("total_potential_profit"))))
    elif bot_id == "trend":
        _replace_metric(card, "Trends detected today", _format_int(_safe_float(metrics.get("trends_detected_today"))))
        topics = metrics.get("top_topics")
        if isinstance(topics, list):
            card["details"] = [
                {"label": str(topic.get("name", "Unknown trend")), "value": _format_score(topic.get("score"))}
                for topic in topics[:5]
                if isinstance(topic, dict)
            ]


def _replace_metric(card: dict[str, Any], label: str, value: str) -> None:
    for metric in card.get("metrics", []):
        if metric.get("label") == label:
            metric["value"] = value
            return
    card.setdefault("metrics", []).append({"label": label, "value": value})
@app.template_filter("json_script")
def json_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)


