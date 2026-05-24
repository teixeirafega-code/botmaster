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


@app.route("/api/status")
def api_status():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(build_snapshot())


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


@app.template_filter("json_script")
def json_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
