from __future__ import annotations

import atexit
import csv
import html
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

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
APP_STARTED_AT = datetime.now(timezone.utc)
logger = logging.getLogger(__name__)
DASHBOARD_TELEGRAM_LAST_SENT: dict[str, float] = {}
TELEGRAM_EVENT_COOLDOWN_SECONDS = 900


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


@app.post("/api/domain-validation-report")
def api_domain_validation_report():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    report = _generate_domain_validation_report(_now())
    return jsonify(
        {
            "ok": True,
            "partial": report["partial"],
            "json_path": report["json_path"],
            "html_path": report["html_path"],
            "summary": report["summary"],
        }
    )


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
    shared_statuses = _read_shared_statuses()
    cards = [
        _yield_card(process_lines, now),
        _domain_card(process_lines, now),
        _asset_card(process_lines, now),
        _trend_card(process_lines, now),
    ]
    cards = _merge_shared_statuses(cards, shared_statuses, now)
    _notify_domain_offline_if_needed(cards)
    running = sum(1 for card in cards if card["status"] == "RUNNING")
    return {
        "generated_at": now.isoformat(),
        "generated_label": _format_dt(now),
        "running_count": running,
        "stopped_count": len(cards) - running,
        "summary": _build_summary(cards, now),
        "domain_safety": _domain_safety_panel(now, shared_statuses),
        "activity_feed": _build_activity_feed(cards, shared_statuses, now),
        "cards": cards,
    }


def _yield_card(process_lines: list[str], now: datetime) -> dict[str, Any]:
    root = _bot_root("yield")
    state = _read_json(root / "app/data/state.json")
    paper_state = _read_json(root / "app/data/paper_state.json")
    log_path = root / "logs/bot.log"
    logs = _tail_lines(log_path, 900)

    current_apys = _latest_yield_apys(state, logs)
    best_protocol = max(
        ((name, value) for name, value in current_apys.items() if value is not None),
        key=lambda item: item[1],
        default=(None, None),
    )
    best_protocol_name = best_protocol[0]
    best_apy = best_protocol[1]
    capital_usd = _yield_capital_usd(paper_state, state)
    estimated_monthly_profit = capital_usd * best_apy / 12 if best_apy is not None else 0.0
    simulated_profit = _yield_simulated_profit(paper_state, logs)
    chart_points = _yield_apy_chart_points(state, now)
    last_update = _latest_timestamp([root / "app/data/state.json", log_path], logs)
    next_scan = _yield_next_scan(root, last_update, now)
    return {
        "id": "yield",
        "name": "Yield Optimizer",
        "accent": "green",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "last_update_ts": last_update.isoformat() if last_update else None,
        "simulated_balance": capital_usd + (simulated_profit or 0.0),
        "potential_profit": estimated_monthly_profit,
        "opportunities_today": 0,
        "best_protocol": _title(best_protocol_name) if best_protocol_name else "No APY data",
        "best_apy": best_apy,
        "best_apy_label": _format_percent(best_apy),
        "estimated_monthly_profit": estimated_monthly_profit,
        "estimated_monthly_profit_label": _format_money(estimated_monthly_profit),
        "capital_label": _format_money(capital_usd),
        "next_scan": next_scan,
        "chart": {
            "label": "Best APY last 24h",
            "points": chart_points,
        },
        "metrics": [
            {"label": "Best protocol", "value": _title(best_protocol_name) if best_protocol_name else "No APY data", "tone": "good"},
            {"label": "Monthly profit", "value": _format_money(estimated_monthly_profit), "tone": "good" if estimated_monthly_profit > 0 else "warning"},
            {"label": "Simulated profit", "value": _format_money(simulated_profit)},
            {"label": "Next scan", "value": next_scan["label"], "tone": next_scan["tone"]},
        ],
        "apys": [
            {"protocol": "Aave", "value": _format_percent(current_apys.get("aave")), "raw": current_apys.get("aave"), "is_best": best_protocol_name == "aave"},
            {"protocol": "Compound", "value": _format_percent(current_apys.get("compound")), "raw": current_apys.get("compound"), "is_best": best_protocol_name == "compound"},
            {"protocol": "Curve", "value": _format_percent(current_apys.get("curve")), "raw": current_apys.get("curve"), "is_best": best_protocol_name == "curve"},
            {"protocol": "Beefy", "value": _format_percent(current_apys.get("beefy")), "raw": current_apys.get("beefy"), "is_best": best_protocol_name == "beefy"},
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
    top_candidates: dict[str, dict[str, Any]] = {}
    inventory_counts = {"registered": 0, "listed": 0, "sold": 0}

    for row in logs:
        event = str(row.get("event_name", "")).upper()
        timestamp = _parse_dt(row.get("timestamp"))
        domain = row.get("domain")
        score = _safe_float(row.get("score"))
        if timestamp and timestamp.astimezone().date() == today and event == "DOMAIN_SCORED":
            scanned_today += 1
            if score is not None and score >= 70:
                opportunities_today += 1
                domain_name = str(domain or "").strip()
                if domain_name:
                    previous = top_candidates.get(domain_name)
                    if not previous or score > previous["score"]:
                        top_candidates[domain_name] = {
                            "name": domain_name,
                            "score": score,
                            "score_label": _format_score(score),
                            "estimated_sale_price": _estimate_domain_sale_price(score),
                            "estimated_sale_price_label": _format_money(_estimate_domain_sale_price(score)),
                            "tone": _score_tone(score),
                        }
        if domain and ("REGISTER" in event or "PURCHASE" in event):
            if last_registered_time is None or (timestamp and timestamp > last_registered_time):
                last_registered = str(domain)
                last_registered_time = timestamp
        if "REGISTER" in event or "PURCHASE" in event:
            inventory_counts["registered"] += 1
        if "LIST" in event:
            inventory_counts["listed"] += 1
        if "SOLD" in event or "SALE" in event:
            inventory_counts["sold"] += 1

    if scanned_today == 0 and isinstance(domains_state, list):
        scanned_today = len(domains_state)
        for item in domains_state:
            if not isinstance(item, dict):
                continue
            score = _safe_float(item.get("score"))
            name = str(item.get("domain") or item.get("name") or "").strip()
            status = str(item.get("status") or "").lower()
            if status in inventory_counts:
                inventory_counts[status] += 1
            if score is not None and score >= 70:
                opportunities_today += 1
                if name:
                    top_candidates[name] = {
                        "name": name,
                        "score": score,
                        "score_label": _format_score(score),
                        "estimated_sale_price": _estimate_domain_sale_price(score),
                        "estimated_sale_price_label": _format_money(_estimate_domain_sale_price(score)),
                        "tone": _score_tone(score),
                    }

    top_domains = sorted(top_candidates.values(), key=lambda item: item["score"], reverse=True)[:5]
    portfolio_value = sum(item["estimated_sale_price"] for item in top_candidates.values())

    last_update = _latest_timestamp([log_path, root / "data/domains.json"], [])
    return {
        "id": "domain",
        "name": "Domain Hunter",
        "accent": "blue",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "last_update_ts": last_update.isoformat() if last_update else None,
        "simulated_balance": portfolio_value,
        "potential_profit": portfolio_value,
        "opportunities_today": opportunities_today,
        "top_domains": top_domains,
        "inventory": {
            "registered": inventory_counts["registered"],
            "listed": inventory_counts["listed"],
            "sold": inventory_counts["sold"],
            "registered_label": _format_int(inventory_counts["registered"]),
            "listed_label": _format_int(inventory_counts["listed"]),
            "sold_label": _format_int(inventory_counts["sold"]),
            "portfolio_value": portfolio_value,
            "portfolio_value_label": _format_money(portfolio_value),
        },
        "metrics": [
            {"label": "Domains scanned today", "value": _format_int(scanned_today), "tone": "neutral"},
            {"label": "Opportunities found", "value": _format_int(opportunities_today), "tone": "good" if opportunities_today else "warning"},
            {"label": "Last domain registered", "value": last_registered or "No registration in logs"},
            {"label": "Portfolio value", "value": _format_money(portfolio_value), "tone": "good" if portfolio_value else "warning"},
        ],
        "details": [],
    }


def _domain_safety_panel(now: datetime, shared_statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    root = _bot_root("domain")
    pending_path = root / "data/pending_approvals.json"
    attempts_path = root / "data/purchase_attempts.json"
    log_path = root / "logs/domain_hunter_bot.log"
    domains_path = root / "data/domains.json"

    approvals = _approval_entries(_read_json(pending_path, default={}))
    attempts = _purchase_attempt_entries(_read_json(attempts_path, default=[]))
    logs = _json_log_tail(log_path, 6000)
    domains_state = _read_json(domains_path, default=[])
    domain_metrics = shared_statuses.get("domain", {}).get("metrics", {})
    metrics = domain_metrics if isinstance(domain_metrics, dict) else {}

    decisions = _domain_decision_rows(logs)
    reason_counts = _rejection_reason_counts(decisions, metrics)
    blocked_counts = _blocked_counts(reason_counts)
    pending = [item for item in approvals if not item["approved"]]
    approved = [item for item in approvals if item["approved"]]
    dry_run_attempts = [item for item in attempts if item["blocked_by_dry_run"]]
    real_attempt_domains = {item["domain"] for item in attempts if item["domain"] and not item["blocked_by_dry_run"]}
    real_purchase_domains, sold_domains = _real_domain_sets(domains_state, logs)
    real_purchase_domains.update(real_attempt_domains)

    reviewed_domains = {
        *(item["domain"] for item in approvals if item["domain"]),
        *(item["domain"] for item in attempts if item["domain"]),
        *(item["domain"] for item in decisions if item["domain"]),
        *real_purchase_domains,
        *sold_domains,
    }
    opportunities_found = max(
        len({item["domain"] for item in decisions if item["domain"]}),
        int(_safe_float(metrics.get("opportunities_found")) or 0),
        len(reviewed_domains),
    )
    approved_domains = {item["domain"] for item in approved if item["domain"]}
    risky_approved = approved_domains.intersection(_blocked_domain_set(decisions, {"trademark", "low_liquidity"}))
    approval_quality = _approval_quality_score(len(approved_domains), len(risky_approved))
    approval_rate = len(approved) / max(1, len(approved) + len(pending))
    capital_protected = _capital_protected(decisions, dry_run_attempts)
    safe_mode = _domain_safety_bool(root, metrics, "safe_mode", "SAFE_MODE", "safe_mode", True)
    dry_run_purchases = _domain_safety_bool(root, metrics, "dry_run_purchases", "DRY_RUN_PURCHASES", "dry_run_purchases", True)

    return {
        "title": "Domain Hunter Safety",
        "safe_mode": safe_mode,
        "safe_mode_label": "ON" if safe_mode else "OFF",
        "safe_mode_tone": "good" if safe_mode else "danger",
        "dry_run_purchases": dry_run_purchases,
        "dry_run_purchases_label": "ON" if dry_run_purchases else "OFF",
        "dry_run_purchases_tone": "good" if dry_run_purchases else "danger",
        "counts": {
            "pending_approvals": len(pending),
            "dry_run_purchase_attempts": len(dry_run_attempts),
            "blocked_by_trademark": blocked_counts["trademark"],
            "blocked_by_low_liquidity": blocked_counts["low_liquidity"],
            "blocked_by_budget": blocked_counts["budget"],
            "blocked_by_cooldown": blocked_counts["cooldown"],
            "manually_approved_domains": len(approved),
            "real_purchases": len(real_purchase_domains),
        },
        "flow": {
            "opportunities_found": opportunities_found,
            "domains_approved": len(approved_domains),
            "domains_purchased": len(real_purchase_domains),
            "domains_sold": len(sold_domains),
        },
        "kpis": [
            {
                "label": "Approval Quality Score",
                "value": f"{approval_quality:.0f}%",
                "tone": "good" if approval_quality >= 85 else "warning" if approval_quality >= 60 else "danger",
            },
            {
                "label": "Trademark Risk Avoided",
                "value": _format_int(blocked_counts["trademark"]),
                "tone": "good" if blocked_counts["trademark"] else "neutral",
            },
            {
                "label": "Capital Protected",
                "value": _format_money(capital_protected),
                "tone": "good" if capital_protected else "neutral",
            },
            {
                "label": "Domains Reviewed",
                "value": _format_int(len(reviewed_domains)),
                "tone": "good" if reviewed_domains else "warning",
            },
        ],
        "charts": {
            "rejection_reasons": _rejection_chart(reason_counts),
            "pending_over_time": _pending_over_time(pending, now),
            "approval_rate": [
                {"label": "Approved", "value": len(approved), "tone": "good"},
                {"label": "Pending", "value": len(pending), "tone": "warning"},
                {"label": "Rate", "value": round(approval_rate * 100, 2), "tone": "good" if approval_rate >= 0.5 else "warning"},
            ],
        },
    }


def _generate_domain_validation_report(now: datetime) -> dict[str, Any]:
    root = _bot_root("domain")
    output_dir = root / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "domain_validation_report.json"
    html_path = output_dir / "domain_validation_report.html"

    pending_path = root / "data/pending_approvals.json"
    attempts_path = root / "data/purchase_attempts.json"
    log_path = root / "logs/domain_hunter_bot.log"
    domains_path = root / "data/domains.json"

    approvals = _approval_entries(_read_json(pending_path, default={}))
    attempts = _purchase_attempt_entries(_read_json(attempts_path, default=[]))
    logs = _json_log_tail(log_path, 8000)
    decisions = _domain_decision_rows(logs)
    domains_state = _read_json(domains_path, default=[])
    cutoff = now - timedelta(days=7)

    window_approvals = [item for item in approvals if _is_in_report_window(item.get("created_at") or item.get("updated_at"), cutoff, now)]
    window_attempts = [item for item in attempts if _is_in_report_window(item.get("timestamp"), cutoff, now)]
    window_decisions = [item for item in decisions if _is_in_report_window(item.get("timestamp"), cutoff, now)]
    window_logs = [item for item in logs if _is_in_report_window(item.get("timestamp"), cutoff, now)]

    reason_counts = _rejection_reason_counts(window_decisions, {})
    blocked_counts = _blocked_counts(reason_counts)
    pending = [item for item in window_approvals if not item["approved"]]
    manual_approvals = [item for item in window_approvals if item["approved"]]
    dry_run_attempts = [item for item in window_attempts if item["blocked_by_dry_run"]]
    real_purchase_domains, sold_domains = _real_domain_sets_in_window(domains_state, window_logs, window_attempts, cutoff, now)
    opportunities_found = _opportunities_in_window(window_logs, window_decisions)
    pending_reviews = [
        _pending_validation_review(item, now, root)
        for item in sorted((entry for entry in approvals if not entry["approved"]), key=lambda row: str(row.get("domain")))
    ]

    timestamps = _validation_report_timestamps(approvals, attempts, decisions, logs, domains_state)
    oldest_timestamp = min(timestamps) if timestamps else None
    partial = oldest_timestamp is None or oldest_timestamp > cutoff
    observed_start = oldest_timestamp if oldest_timestamp and oldest_timestamp > cutoff else cutoff
    observed_days = max(0.0, (now - observed_start).total_seconds() / 86_400) if oldest_timestamp else 0.0

    summary = {
        "opportunities_found": len(opportunities_found),
        "pending_approvals": len(pending),
        "dry_run_purchase_attempts": len(dry_run_attempts),
        "trademark_blocks": blocked_counts["trademark"],
        "liquidity_blocks": blocked_counts["low_liquidity"],
        "budget_cooldown_blocks": blocked_counts["budget"] + blocked_counts["cooldown"],
        "manual_approvals": len(manual_approvals),
        "real_purchases": len(real_purchase_domains),
        "sold_domains": len(sold_domains),
    }
    report = {
        "report_name": "Domain Hunter 7-Day Validation Report",
        "generated_at": now.isoformat(),
        "period_start": cutoff.isoformat(),
        "period_end": now.isoformat(),
        "requested_days": 7,
        "observed_days": round(observed_days, 2),
        "partial": partial,
        "partial_reason": "Fewer than 7 days of timestamped Domain Hunter data were found." if partial else "",
        "safety_rules": {
            "never_triggers_real_purchases": True,
            "dry_run_attempts_counted_as_real_purchases": False,
            "pending_approvals_counted_as_owned_domains": False,
        },
        "summary": summary,
        "pending_domain_reviews": pending_reviews,
        "artifacts": {
            "json": str(json_path),
            "html": str(html_path),
        },
        "json_path": str(json_path),
        "html_path": str(html_path),
    }
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    html_path.write_text(_domain_validation_report_html(report), encoding="utf-8")
    return report


def _pending_validation_review(item: dict[str, Any], now: datetime, root: Path) -> dict[str, Any]:
    created_at = _parse_dt(item.get("created_at")) or _parse_dt(item.get("updated_at")) or now
    age_days = max(0.0, (now - created_at.astimezone(timezone.utc)).total_seconds() / 86_400)
    cooldown_minutes = _domain_risk_number(root, "cooldown_minutes_between_buys", "COOLDOWN_MINUTES_BETWEEN_BUYS", 120.0)
    min_score = _domain_risk_number(root, "min_score_to_buy", "MIN_SCORE_TO_BUY", 88.0)
    min_expected_value = _domain_risk_number(root, "min_expected_value", "MIN_EXPECTED_VALUE", 250.0)
    score = _safe_float(item.get("score"))
    expected_value = _safe_float(item.get("expected_value"))
    liquidity_grade = str(item.get("liquidity_grade") or "").upper()
    trademark_risk = item.get("trademark_risk") is True
    reviewed_after_cooldown = age_days * 24 * 60 >= cooldown_minutes
    score_ok = score is not None and score >= min_score
    value_ok = expected_value is not None and expected_value >= min_expected_value
    liquidity_ok = liquidity_grade in {"A", "B"}
    still_worth_buying = bool(score_ok and value_ok and liquidity_ok and not trademark_risk)

    if trademark_risk:
        final_decision = "reject"
        reviewer_notes = "Auto-review: trademark risk remains present after the validation window."
    elif not liquidity_ok or not value_ok or not score_ok:
        final_decision = "reject"
        reviewer_notes = "Auto-review: score, liquidity, or expected value no longer clears the buy policy."
    elif reviewed_after_cooldown and still_worth_buying:
        final_decision = "approve"
        reviewer_notes = "Auto-review: still clears score, liquidity, expected value, and cooldown checks. Manual approval is still required."
    else:
        final_decision = "keep watching"
        reviewer_notes = "Auto-review: candidate still needs more cooldown/observation time before a decision."

    return {
        "domain": item["domain"],
        "review_question": "Would I still approve this after 7 days?",
        "created_at": created_at.isoformat(),
        "age_days": round(age_days, 2),
        "score": score,
        "expected_value": expected_value,
        "sale_probability": _safe_float(item.get("sale_probability")),
        "liquidity_grade": liquidity_grade or "unknown",
        "trademark_risk": trademark_risk,
        "reviewed_after_cooldown": reviewed_after_cooldown,
        "still_worth_buying": still_worth_buying,
        "reviewer_notes": reviewer_notes,
        "final_decision": final_decision,
    }


def _domain_validation_report_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    reviews = report.get("pending_domain_reviews", [])
    partial_badge = "PARTIAL" if report.get("partial") else "FULL 7 DAYS"
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('domain', '')))}</td>"
        f"<td>{html.escape(str(item.get('age_days', '')))}</td>"
        f"<td>{html.escape(str(item.get('liquidity_grade', '')))}</td>"
        f"<td>{html.escape(str(item.get('expected_value', '')))}</td>"
        f"<td>{html.escape(str(item.get('reviewed_after_cooldown', '')))}</td>"
        f"<td>{html.escape(str(item.get('still_worth_buying', '')))}</td>"
        f"<td>{html.escape(str(item.get('final_decision', '')))}</td>"
        f"<td>{html.escape(str(item.get('reviewer_notes', '')))}</td>"
        "</tr>"
        for item in reviews
    ) or '<tr><td colspan="8">No pending domains to review.</td></tr>'
    summary_cards = "\n".join(
        f"<div class='card'><span>{html.escape(_title(key))}</span><strong>{html.escape(str(value))}</strong></div>"
        for key, value in summary.items()
    )
    partial_note = (
        f"<p class='warning'>{html.escape(str(report.get('partial_reason')))}</p>"
        if report.get("partial")
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Domain Hunter 7-Day Validation Report</title>
  <style>
    body {{ margin:0; background:#07101b; color:#e5eefb; font-family: Inter, Arial, sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 18px 44px; }}
    h1 {{ margin:0 0 8px; font-size:28px; }}
    .muted {{ color:#93a4ba; }}
    .badge {{ display:inline-block; padding:6px 10px; border-radius:999px; background:#102033; border:1px solid #26364b; font-weight:800; }}
    .warning {{ color:#fbbf24; }}
    .grid {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; margin:20px 0; }}
    .card {{ border:1px solid #26364b; border-radius:8px; padding:12px; background:#0d1726; }}
    .card span {{ display:block; color:#93a4ba; font-size:12px; text-transform:uppercase; }}
    .card strong {{ display:block; margin-top:7px; font-size:24px; }}
    table {{ width:100%; border-collapse:collapse; margin-top:14px; background:#0d1726; border-radius:8px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid #26364b; padding:10px; text-align:left; vertical-align:top; }}
    th {{ color:#93a4ba; font-size:12px; text-transform:uppercase; }}
    @media (max-width: 780px) {{ .grid {{ grid-template-columns:1fr; }} table {{ font-size:12px; }} }}
  </style>
</head>
<body>
  <main>
    <span class="badge">{html.escape(partial_badge)}</span>
    <h1>Domain Hunter 7-Day Validation Report</h1>
    <p class="muted">Generated at {html.escape(str(report.get('generated_at')))}. Period: {html.escape(str(report.get('period_start')))} to {html.escape(str(report.get('period_end')))}.</p>
    {partial_note}
    <p class="muted">This report never triggers real purchases. Dry-run attempts are not counted as real purchases, and pending approvals are not owned domains.</p>
    <section class="grid">{summary_cards}</section>
    <h2>Pending Domain Review</h2>
    <p class="muted">Review question for each pending domain: Would I still approve this after 7 days?</p>
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          <th>Age Days</th>
          <th>Liquidity</th>
          <th>Expected Value</th>
          <th>After Cooldown</th>
          <th>Still Worth Buying</th>
          <th>Final Decision</th>
          <th>Reviewer Notes</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </main>
</body>
</html>
"""


def _is_in_report_window(value: Any, cutoff: datetime, now: datetime) -> bool:
    parsed = _parse_dt(value)
    if not parsed:
        return False
    parsed = parsed.astimezone(timezone.utc)
    return cutoff <= parsed <= now


def _opportunities_in_window(logs: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> set[str]:
    domains = {item["domain"] for item in decisions if item.get("domain")}
    for row in logs:
        event = str(row.get("event_name") or "").upper()
        score = _safe_float(row.get("score"))
        domain = str(row.get("domain") or "").strip().lower()
        if domain and event == "DOMAIN_SCORED" and score is not None and score >= 70:
            domains.add(domain)
    return domains


def _real_domain_sets_in_window(
    domains_state: Any,
    logs: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    cutoff: datetime,
    now: datetime,
) -> tuple[set[str], set[str]]:
    purchased, sold = _real_domain_sets([], logs)
    for item in attempts:
        if item.get("domain") and not item.get("blocked_by_dry_run") and _is_in_report_window(item.get("timestamp"), cutoff, now):
            purchased.add(str(item["domain"]))
    if isinstance(domains_state, list):
        for item in domains_state:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or item.get("name") or "").strip().lower()
            status = str(item.get("status") or "").lower()
            registered_at = item.get("registered_at") or item.get("listed_at") or item.get("updated_at")
            sold_at = item.get("sold_at") or item.get("updated_at")
            if domain and status in {"registered", "listed", "sold"} and _is_in_report_window(registered_at, cutoff, now):
                purchased.add(domain)
            if domain and status == "sold" and _is_in_report_window(sold_at, cutoff, now):
                sold.add(domain)
    return purchased, sold


def _validation_report_timestamps(
    approvals: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    domains_state: Any,
) -> list[datetime]:
    timestamps: list[datetime] = []
    for item in approvals:
        parsed = _parse_dt(item.get("created_at") or item.get("updated_at"))
        if parsed:
            timestamps.append(parsed)
    for item in attempts:
        parsed = _parse_dt(item.get("timestamp"))
        if parsed:
            timestamps.append(parsed)
    for item in decisions:
        parsed = _parse_dt(item.get("timestamp"))
        if parsed:
            timestamps.append(parsed)
    for item in logs:
        parsed = _parse_dt(item.get("timestamp"))
        if parsed:
            timestamps.append(parsed)
    if isinstance(domains_state, list):
        for item in domains_state:
            if not isinstance(item, dict):
                continue
            for key in ("registered_at", "listed_at", "sold_at", "updated_at"):
                parsed = _parse_dt(item.get(key))
                if parsed:
                    timestamps.append(parsed)
    return [item.astimezone(timezone.utc) for item in timestamps]


def _domain_risk_number(root: Path, key: str, env_key: str, default: float) -> float:
    env_value = os.getenv(env_key)
    if env_value is None:
        env_value = _read_env_value(root / ".env", env_key)
    value = _safe_float(env_value)
    if value is not None:
        return value
    config_value = _read_risk_config_number(root / "config.yaml", key)
    return default if config_value is None else config_value


def _read_risk_config_number(path: Path, key: str) -> float | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    risk_match = re.search(r"^risk:\s*$([\s\S]*?)(?=^[A-Za-z_][\w-]*:\s*$|\Z)", text, re.MULTILINE)
    risk_text = risk_match.group(1) if risk_match else text
    match = re.search(rf"^\s+{re.escape(key)}\s*:\s*([-+]?[0-9]*\.?[0-9]+)", risk_text, re.MULTILINE)
    return _safe_float(match.group(1)) if match else None


def _approval_entries(raw: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        iterable = raw.items()
    elif isinstance(raw, list):
        iterable = [(None, item) for item in raw]
    else:
        iterable = []
    for key, value in iterable:
        if not isinstance(value, dict):
            continue
        domain = str(value.get("domain") or key or "").strip().lower()
        if not domain:
            continue
        entries.append(
            {
                "domain": domain,
                "approved": value.get("approved") is True,
                "score": _safe_float(value.get("score")),
                "price": _safe_float(value.get("price")),
                "expected_value": _safe_float(value.get("expected_value")),
                "sale_probability": _safe_float(value.get("sale_probability")),
                "liquidity_grade": str(value.get("liquidity_grade") or "").strip().upper(),
                "trademark_risk": value.get("trademark_risk") is True,
                "created_at": value.get("created_at") or value.get("updated_at") or value.get("timestamp"),
                "updated_at": value.get("updated_at") or value.get("created_at") or value.get("timestamp"),
            }
        )
    return entries


def _purchase_attempt_entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    entries = []
    for value in raw:
        if not isinstance(value, dict):
            continue
        domain = str(value.get("domain") or "").strip().lower()
        entries.append(
            {
                "domain": domain,
                "price": _safe_float(value.get("price")) or 0.0,
                "blocked_by_dry_run": value.get("blocked_by_dry_run") is True,
                "timestamp": value.get("timestamp"),
            }
        )
    return entries


def _domain_decision_rows(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for row in logs:
        event = str(row.get("event_name") or "").lower()
        decision = str(row.get("decision") or "").lower()
        reason = str(row.get("decision_reason") or row.get("reason") or "").strip().lower()
        if event != "acquisition_decision" and not reason.startswith(("reject:", "watchlist:", "buy:")):
            continue
        if ":" in reason and reason.split(":", 1)[0] in {"reject", "watchlist", "buy"}:
            decision, reason = reason.split(":", 1)
        decisions.append(
            {
                "domain": str(row.get("domain") or "").strip().lower(),
                "decision": decision,
                "reason": reason,
                "price": _safe_float(row.get("price")) or 0.0,
                "timestamp": row.get("timestamp"),
            }
        )
    return decisions


def _rejection_reason_counts(decisions: list[dict[str, Any]], metrics: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in decisions:
        if item["decision"] == "reject" and item["reason"]:
            counts[item["reason"]] += 1
    policy_counters = metrics.get("policy_counters")
    if isinstance(policy_counters, dict):
        for key, value in policy_counters.items():
            key_text = str(key)
            if not key_text.startswith("reason:"):
                continue
            reason = key_text.split(":", 1)[1]
            if _safety_rejection_category(reason) == "other":
                continue
            counts[reason] = max(counts[reason], int(_safe_float(value) or 0))
    return counts


def _blocked_counts(reason_counts: Counter[str]) -> Counter[str]:
    blocked: Counter[str] = Counter()
    for reason, count in reason_counts.items():
        category = _safety_rejection_category(reason)
        if category != "other":
            blocked[category] += count
    return blocked


def _blocked_domain_set(decisions: list[dict[str, Any]], categories: set[str]) -> set[str]:
    domains = set()
    for item in decisions:
        if item["decision"] == "reject" and _safety_rejection_category(item["reason"]) in categories and item["domain"]:
            domains.add(item["domain"])
    return domains


def _safety_rejection_category(reason: str) -> str:
    text = str(reason).lower()
    if "trademark" in text or "brand" in text:
        return "trademark"
    if "liquidity" in text or "expected_value" in text or "resale_probability" in text:
        return "low_liquidity"
    if any(token in text for token in ("budget", "spend", "price_above", "max_buys", "max_portfolio", "capital_exposure")):
        return "budget"
    if "cooldown" in text:
        return "cooldown"
    return "other"


def _real_domain_sets(domains_state: Any, logs: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    purchased: set[str] = set()
    sold: set[str] = set()
    if isinstance(domains_state, list):
        for item in domains_state:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or item.get("name") or "").strip().lower()
            status = str(item.get("status") or "").lower()
            if domain and status in {"registered", "listed", "sold"}:
                purchased.add(domain)
            if domain and status == "sold":
                sold.add(domain)
    for row in logs:
        event = str(row.get("event_name") or "").lower()
        domain = str(row.get("domain") or "").strip().lower()
        if not domain:
            continue
        if event == "domain_registered":
            purchased.add(domain)
        if event in {"domain_sold", "sale", "sale_recorded"} or "sold" in event:
            sold.add(domain)
            purchased.add(domain)
    return purchased, sold


def _capital_protected(decisions: list[dict[str, Any]], dry_run_attempts: list[dict[str, Any]]) -> float:
    total = sum(item["price"] for item in dry_run_attempts)
    for item in decisions:
        if item["decision"] == "reject" and _safety_rejection_category(item["reason"]) != "other":
            total += _safe_float(item.get("price")) or 0.0
    return total


def _approval_quality_score(approved_count: int, risky_approved_count: int) -> float:
    if approved_count <= 0:
        return 0.0
    return max(0.0, 100.0 - (risky_approved_count / approved_count * 100.0))


def _rejection_chart(reason_counts: Counter[str]) -> list[dict[str, Any]]:
    rows = []
    for reason, value in reason_counts.most_common(8):
        category = _safety_rejection_category(reason)
        rows.append({"label": _title(reason), "value": value, "tone": _safety_tone(category)})
    return rows


def _pending_over_time(pending: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    buckets = {
        (now.astimezone().date() - timedelta(days=offset)).isoformat(): 0
        for offset in range(13, -1, -1)
    }
    for item in pending:
        parsed = _parse_dt(item.get("created_at")) or now
        key = parsed.astimezone().date().isoformat()
        if key in buckets:
            buckets[key] += 1
    return [{"label": key[5:], "value": value, "tone": "warning" if value else "neutral"} for key, value in buckets.items()]


def _safety_tone(category: str) -> str:
    if category in {"trademark", "budget"}:
        return "danger"
    if category in {"low_liquidity", "cooldown"}:
        return "warning"
    return "neutral"


def _domain_safety_bool(root: Path, metrics: dict[str, Any], metric_key: str, env_key: str, config_key: str, default: bool) -> bool:
    if metric_key in metrics:
        return _coerce_bool(metrics.get(metric_key), default)
    env_value = os.getenv(env_key)
    if env_value is None:
        env_value = _read_env_value(root / ".env", env_key)
    if env_value is not None:
        return _coerce_bool(env_value, default)
    config_value = _read_config_bool(root / "config.yaml", config_key)
    return default if config_value is None else config_value


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip().strip('"').strip("'") if match else None


def _read_config_bool(path: Path, key: str) -> bool | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*(true|false|yes|no|on|off|1|0)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return _coerce_bool(match.group(1), False) if match else None


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


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

    top_opportunities = _asset_opportunities(stats)
    opportunities = len(top_opportunities)
    if opportunities == 0:
        opportunities = _first_number(stats, "opportunities_found")

    total_profit = sum(item["profit_potential"] for item in top_opportunities)
    if total_profit <= 0:
        total_profit = _first_number(stats, "total_potential_profit") or 0.0

    last_update = _latest_timestamp([stats_path, state_path, log_path], [])
    return {
        "id": "asset",
        "name": "Asset Flip",
        "accent": "amber",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "last_update_ts": last_update.isoformat() if last_update else None,
        "simulated_balance": 0.0,
        "potential_profit": total_profit,
        "opportunities_today": opportunities,
        "score_filter_min": 70,
        "top_opportunities": top_opportunities[:12],
        "metrics": [
            {"label": "Assets scanned", "value": _format_int(assets_scanned), "tone": "neutral"},
            {"label": "Opportunities found", "value": _format_int(opportunities), "tone": "good" if opportunities else "warning"},
            {"label": "Total potential profit", "value": _format_money(total_profit), "tone": "good" if total_profit else "warning"},
        ],
        "details": [
            {"label": item["name"], "value": item["profit_label"]}
            for item in top_opportunities[:3]
        ],
    }


def _trend_card(process_lines: list[str], now: datetime) -> dict[str, Any]:
    root = _bot_root("trend")
    db_path = root / "trend_hunter.db"
    log_path = root / "logs/trend_hunter.log"
    trends_today, top_topics = _read_trend_db(db_path, now)
    if trends_today is None:
        trends_today, top_topics = _read_trend_logs(log_path, now)

    last_update = _latest_timestamp([db_path, log_path], [])
    top_score = max((_safe_float(topic.get("score")) or 0.0 for topic in top_topics), default=0.0)
    return {
        "id": "trend",
        "name": "Trend Hunter",
        "accent": "purple",
        "status": _bot_status(root, process_lines, last_update, now),
        "last_update": _format_dt(last_update),
        "last_update_ts": last_update.isoformat() if last_update else None,
        "simulated_balance": 0.0,
        "potential_profit": 0.0,
        "opportunities_today": trends_today or 0,
        "top_trends": [_trend_display_item(topic) for topic in top_topics[:5]],
        "metrics": [
            {"label": "Trends detected today", "value": _format_int(trends_today), "tone": "good" if trends_today else "warning"},
            {"label": "Top score", "value": _format_score(top_score), "tone": _score_tone(top_score)},
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


def _yield_capital_usd(paper_state: dict[str, Any], state: dict[str, Any]) -> float:
    for source, key in ((paper_state, "wallet_balances"), (state, "holdings")):
        balances = source.get(key)
        if not isinstance(balances, dict):
            continue
        total = 0.0
        for symbol in ("USDC", "USDT", "DAI"):
            total += _stablecoin_units_to_usd(_safe_float(balances.get(symbol)) or 0.0)
        if total > 0:
            return total
    return 140.0


def _stablecoin_units_to_usd(value: float) -> float:
    if value > 100_000:
        return value / 1_000_000
    return value


def _yield_simulated_profit(paper_state: dict[str, Any], logs: list[str]) -> float | None:
    analytics = paper_state.get("analytics") if isinstance(paper_state.get("analytics"), dict) else {}
    value = _first_number(
        analytics,
        "hypothetical_pnl_usd",
        "realized_simulated_yield_usd",
        "accrued_simulated_yield_usd",
    )
    if value is not None:
        return value
    return _latest_number_from_logs(
        logs,
        ["expected_profit_usd", "pnl_usd", "accrued_simulated_yield_usd"],
    )


def _yield_apy_chart_points(state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    records = []
    history = state.get("apy_history", {})
    if isinstance(history, dict):
        raw = history.get("USDC")
        if isinstance(raw, list):
            records = [item for item in raw if isinstance(item, dict)]

    grouped: dict[int, dict[str, Any]] = {}
    cutoff = now.timestamp() - 86_400
    for record in records:
        ts = _safe_float(record.get("ts"))
        apy = _safe_float(record.get("apy"))
        protocol = str(record.get("protocol") or "").lower()
        if ts is None or apy is None or not protocol:
            continue
        if ts < cutoff and len(records) > 80:
            continue
        bucket = int(ts // 300) * 300
        current = grouped.get(bucket)
        if not current or apy > current["apy"]:
            grouped[bucket] = {"ts": ts, "apy": apy, "protocol": protocol}

    points = sorted(grouped.values(), key=lambda item: item["ts"])
    if not points and records:
        fallback = sorted(records, key=lambda item: _safe_float(item.get("ts")) or 0)[-80:]
        for record in fallback:
            ts = _safe_float(record.get("ts"))
            apy = _safe_float(record.get("apy"))
            protocol = str(record.get("protocol") or "").lower()
            if ts is not None and apy is not None and protocol:
                points.append({"ts": ts, "apy": apy, "protocol": protocol})

    return [
        {
            "time": datetime.fromtimestamp(item["ts"], tz=timezone.utc).astimezone().strftime("%H:%M"),
            "value": round(item["apy"] * 100, 4),
            "value_label": _format_percent(item["apy"]),
            "protocol": _title(item["protocol"]),
        }
        for item in points[-80:]
    ]


def _yield_next_scan(root: Path, last_update: datetime | None, now: datetime) -> dict[str, Any]:
    interval = _read_config_number(root / "config.yaml", "monitor_interval_seconds", 60)
    if not last_update:
        return {"label": "Ready now", "seconds": 0, "tone": "good"}
    remaining = int((last_update.astimezone(timezone.utc).timestamp() + interval) - now.timestamp())
    if remaining <= 0:
        return {"label": "Ready now", "seconds": 0, "tone": "good"}
    return {"label": _human_duration(remaining), "seconds": remaining, "tone": "warning" if remaining > 30 else "good"}


def _read_config_number(path: Path, key: str, default: int) -> int:
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*([0-9]+)", text, re.MULTILINE)
    return int(match.group(1)) if match else default


def _asset_opportunities(stats: dict[str, Any]) -> list[dict[str, Any]]:
    raw = stats.get("opportunities") if isinstance(stats.get("opportunities"), list) else []
    opportunities: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        listing = item.get("listing") if isinstance(item.get("listing"), dict) else {}
        valuation = item.get("valuation") if isinstance(item.get("valuation"), dict) else {}
        name = str(listing.get("name") or "Unnamed asset").strip()
        url = str(listing.get("url") or "").strip()
        asking_price = _safe_float(listing.get("asking_price")) or 0.0
        real_value = _safe_float(valuation.get("estimated_real_value")) or 0.0
        profit = _safe_float(valuation.get("profit_potential")) or 0.0
        score = _safe_float(item.get("opportunity_score")) or 0.0
        if not _is_asset_opportunity_sane(name, url, asking_price, real_value, profit):
            continue
        profit_percent = (profit / asking_price * 100) if asking_price > 0 else 0.0
        opportunities.append(
            {
                "name": name[:120],
                "url": url,
                "marketplace": _title(str(listing.get("marketplace") or "marketplace")),
                "score": score,
                "score_label": _format_score(score),
                "tone": _score_tone(score),
                "asking_price": asking_price,
                "real_value": real_value,
                "profit_potential": profit,
                "profit_percent": profit_percent,
                "asking_label": _format_money(asking_price),
                "value_label": _format_money(real_value),
                "profit_label": _format_money(profit),
                "profit_percent_label": f"{profit_percent:.1f}%",
                "detected_at": item.get("detected_at"),
            }
        )
    return sorted(opportunities, key=lambda row: (row["score"], row["profit_potential"]), reverse=True)


def _is_asset_opportunity_sane(name: str, url: str, asking_price: float, real_value: float, profit: float) -> bool:
    generic_names = {"view listing", "read more", "pricing", "websites", "blog", "sales@empireflippers.com"}
    normalized = name.strip().lower()
    if not url.startswith("http") or normalized in generic_names:
        return False
    if asking_price <= 0 or real_value <= 0 or profit <= 0:
        return False
    return asking_price <= 50_000_000 and real_value <= 250_000_000


def _estimate_domain_sale_price(score: float) -> float:
    if score >= 95:
        return 12_500.0
    if score >= 90:
        return 7_500.0
    if score >= 85:
        return 3_500.0
    if score >= 80:
        return 1_500.0
    if score >= 75:
        return 750.0
    return 299.0


def _trend_display_item(topic: dict[str, Any]) -> dict[str, Any]:
    score = _safe_float(topic.get("score")) or 0.0
    components = topic.get("component_scores") if isinstance(topic.get("component_scores"), dict) else {}
    growth_velocity = _safe_float(components.get("growth_velocity"))
    platforms = topic.get("platforms") if isinstance(topic.get("platforms"), list) else []
    velocity = _trend_velocity(score, growth_velocity)
    return {
        "name": str(topic.get("name") or "Unknown trend")[:150],
        "score": score,
        "score_label": _format_score(score),
        "score_width": max(3, min(100, score)),
        "tone": _score_tone(score),
        "platforms": [str(platform) for platform in platforms],
        "platform_badges": _platform_badges(platforms),
        "velocity": velocity["label"],
        "velocity_tone": velocity["tone"],
        "observed_at": topic.get("observed_at"),
        "signal_count": topic.get("signal_count"),
    }


def _trend_velocity(score: float, growth_velocity: float | None) -> dict[str, str]:
    velocity = growth_velocity if growth_velocity is not None else score
    if velocity >= 100 or score >= 80:
        return {"label": "SURGING", "tone": "good"}
    if velocity >= 50 or score >= 60:
        return {"label": "RISING", "tone": "warning"}
    return {"label": "WATCH", "tone": "danger"}


def _platform_badges(platforms: list[Any]) -> list[dict[str, str]]:
    labels = {
        "reddit": "R",
        "google": "G",
        "google_trends": "G",
        "twitter": "X",
        "x": "X",
        "tiktok": "TT",
    }
    badges = []
    for platform in platforms:
        key = str(platform).lower()
        badges.append({"label": labels.get(key, key[:2].upper()), "name": _title(key)})
    return badges or [{"label": "NA", "name": "Unknown"}]


def _score_tone(score: float | None) -> str:
    if score is None:
        return "neutral"
    if score >= 75:
        return "good"
    if score >= 50:
        return "warning"
    return "danger"


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
                SELECT name, score, component_scores_json, platforms_json, observed_at, signal_count
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
                "component_scores": _json_loads(row["component_scores_json"], {}),
                "platforms": _json_loads(row["platforms_json"], []),
                "observed_at": row["observed_at"],
                "signal_count": row["signal_count"],
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
            top.append(
                {
                    "name": trend_match.group(1),
                    "score": _safe_float(trend_match.group(2)),
                    "platforms": ["reddit"],
                    "observed_at": timestamp.isoformat() if timestamp else None,
                }
            )
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
    render_disk = Path("/var/data")
    if render_disk.exists():
        return render_disk / "botmaster_status.sqlite"
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
            card["last_update_ts"] = updated_at.isoformat()
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
                if key in apys:
                    item["raw"] = _safe_float(apys.get(key))
                    item["value"] = _format_percent(item["raw"])
            best = max(
                ((str(item.get("protocol", "")).lower(), _safe_float(item.get("raw"))) for item in card.get("apys", [])),
                key=lambda row: row[1] if row[1] is not None else -1,
                default=(None, None),
            )
            if best[0] and best[1] is not None:
                card["best_protocol"] = _title(best[0])
                card["best_apy"] = best[1]
                card["best_apy_label"] = _format_percent(best[1])
                for item in card.get("apys", []):
                    item["is_best"] = str(item.get("protocol", "")).lower() == best[0]
        if metrics.get("best_protocol"):
            card["best_protocol"] = _title(str(metrics.get("best_protocol")))
            _replace_metric(card, "Best protocol", card["best_protocol"], "good")
        if "simulated_profit" in metrics:
            _replace_metric(card, "Simulated profit", _format_money(_safe_float(metrics.get("simulated_profit"))))
    elif bot_id == "domain":
        if "domains_scanned_today" in metrics:
            _replace_metric(card, "Domains scanned today", _format_int(_safe_float(metrics.get("domains_scanned_today"))))
        if "opportunities_found" in metrics:
            opportunities = _safe_float(metrics.get("opportunities_found"))
            card["opportunities_today"] = int(opportunities or 0)
            _replace_metric(card, "Opportunities found", _format_int(opportunities), "good" if opportunities else "warning")
        if "domains_registered" in metrics:
            registered = int(_safe_float(metrics.get("domains_registered")) or 0)
            card.setdefault("inventory", {})["registered"] = registered
            card["inventory"]["registered_label"] = _format_int(registered)
        if metrics.get("last_domain_registered"):
            _replace_metric(card, "Last domain registered", str(metrics.get("last_domain_registered")))
    elif bot_id == "asset":
        if "assets_scanned" in metrics:
            _replace_metric(card, "Assets scanned", _format_int(_safe_float(metrics.get("assets_scanned"))))
        if "opportunities_found" in metrics:
            opportunities = _safe_float(metrics.get("opportunities_found"))
            _replace_metric(card, "Opportunities found", _format_int(opportunities), "good" if opportunities else "warning")
        if "total_potential_profit" in metrics:
            total_profit = _safe_float(metrics.get("total_potential_profit"))
            if total_profit is not None and total_profit > (card.get("potential_profit") or 0):
                card["potential_profit"] = total_profit
                _replace_metric(card, "Total potential profit", _format_money(total_profit), "good" if total_profit else "warning")
    elif bot_id == "trend":
        if "trends_detected_today" in metrics:
            trends = _safe_float(metrics.get("trends_detected_today"))
            card["opportunities_today"] = int(trends or 0)
            _replace_metric(card, "Trends detected today", _format_int(trends), "good" if trends else "warning")
        topics = metrics.get("top_topics")
        if isinstance(topics, list):
            display_topics = [_trend_display_item(topic) for topic in topics[:5] if isinstance(topic, dict)]
            card["top_trends"] = display_topics
            card["details"] = [{"label": topic["name"], "value": topic["score_label"]} for topic in display_topics]


def _replace_metric(card: dict[str, Any], label: str, value: str, tone: str | None = None) -> None:
    for metric in card.get("metrics", []):
        if metric.get("label") == label:
            metric["value"] = value
            if tone:
                metric["tone"] = tone
            return
    item = {"label": label, "value": value}
    if tone:
        item["tone"] = tone
    card.setdefault("metrics", []).append(item)


def _build_summary(cards: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    total_balance = sum(_safe_float(card.get("simulated_balance")) or 0.0 for card in cards)
    total_opportunities = sum(int(_safe_float(card.get("opportunities_today")) or 0) for card in cards)
    total_profit = sum(_safe_float(card.get("potential_profit")) or 0.0 for card in cards)
    running = sum(1 for card in cards if card.get("status") == "RUNNING")
    uptime_seconds = max(0, int((now - APP_STARTED_AT).total_seconds()))
    return [
        {
            "label": "Total Simulated Balance",
            "value": _format_money(total_balance),
            "caption": "Paper capital plus marked portfolio value",
            "tone": "good" if total_balance > 0 else "warning",
        },
        {
            "label": "Total Opportunities Today",
            "value": _format_int(total_opportunities),
            "caption": "Domains, assets and trends detected",
            "tone": "good" if total_opportunities else "warning",
        },
        {
            "label": "Total Potential Profit",
            "value": _format_money(total_profit),
            "caption": "Modeled upside from active signals",
            "tone": "good" if total_profit > 0 else "warning",
        },
        {
            "label": "Uptime",
            "value": _human_duration(uptime_seconds),
            "caption": f"{running}/{len(cards)} bots online",
            "tone": "good" if running == len(cards) else "danger" if running == 0 else "warning",
        },
    ]


def _build_activity_feed(
    cards: list[dict[str, Any]],
    shared_statuses: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def add_event(bot_id: str, bot_name: str, message: str, timestamp: Any, severity: str = "info") -> None:
        parsed = _parse_dt(timestamp) or now
        events.append(
            {
                "bot_id": bot_id,
                "bot": bot_name,
                "message": message[:220],
                "time": _format_dt(parsed),
                "timestamp": parsed.isoformat(),
                "tone": _severity_tone(severity),
            }
        )

    for card in cards:
        status = str(card.get("status") or "UNKNOWN")
        add_event(str(card["id"]), str(card["name"]), f"Status heartbeat: {status}", card.get("last_update_ts"), status)

    for bot_id, status in shared_statuses.items():
        if status.get("error"):
            add_event(bot_id, BOT_CONFIG.get(bot_id, {}).get("label", bot_id), f"Supervisor error: {status['error']}", status.get("updated_at"), "error")

    domain_root = _bot_root("domain")
    for row in _json_log_tail(domain_root / "logs/domain_hunter_bot.log", 120)[-40:]:
        event = str(row.get("event_name") or row.get("message") or "domain_event")
        domain = row.get("domain")
        score = row.get("score")
        message = event
        if domain:
            message += f" | {domain}"
        if score is not None:
            message += f" | score {_format_score(score)}"
        add_event("domain", "Domain Hunter", message, row.get("timestamp"), row.get("severity", "info"))

    asset_card = next((card for card in cards if card.get("id") == "asset"), None)
    if asset_card:
        for item in asset_card.get("top_opportunities", [])[:8]:
            add_event(
                "asset",
                "Asset Flip",
                f"Opportunity {item.get('score_label')} | {item.get('name')} | upside {item.get('profit_label')}",
                item.get("detected_at") or asset_card.get("last_update_ts"),
                item.get("tone", "info"),
            )

    trend_card = next((card for card in cards if card.get("id") == "trend"), None)
    if trend_card:
        for item in trend_card.get("top_trends", [])[:8]:
            platforms = ", ".join(item.get("platforms") or [])
            suffix = f" | {platforms}" if platforms else ""
            add_event(
                "trend",
                "Trend Hunter",
                f"{item.get('velocity')} trend {item.get('score_label')} | {item.get('name')}{suffix}",
                item.get("observed_at") or trend_card.get("last_update_ts"),
                item.get("velocity_tone", "info"),
            )

    yield_card = next((card for card in cards if card.get("id") == "yield"), None)
    if yield_card and yield_card.get("chart", {}).get("points"):
        point = yield_card["chart"]["points"][-1]
        add_event(
            "yield",
            "Yield Optimizer",
            f"APY sample | {point.get('protocol')} at {point.get('value_label')}",
            yield_card.get("last_update_ts"),
            "good",
        )

    events.sort(key=lambda item: item["timestamp"], reverse=True)
    return events[:20]


def _severity_tone(severity: str) -> str:
    text = str(severity).lower()
    if text in {"running", "good", "info", "ok", "success"}:
        return "good"
    if text in {"warning", "warn", "stopped"}:
        return "warning"
    if text in {"error", "critical", "danger", "failed"}:
        return "danger"
    return "neutral"


def _human_duration(seconds: float | int) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, seconds = divmod(remaining, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _notify_domain_offline_if_needed(cards: list[dict[str, Any]]) -> None:
    domain_card = next((card for card in cards if card.get("id") == "domain"), None)
    if not domain_card or domain_card.get("status") == "RUNNING":
        return
    _dashboard_telegram_alert(
        "bot_offline_detection",
        "Bot offline detection\n"
        "Bot: Domain Hunter\n"
        f"Status: {domain_card.get('status', 'UNKNOWN')}\n"
        f"Last update: {domain_card.get('last_update', 'Unknown')}",
    )


def _dashboard_telegram_alert(event_type: str, message: str, *, disable_notification: bool = False) -> bool:
    if not _dashboard_telegram_enabled():
        return False
    token = _dashboard_telegram_setting("TELEGRAM_BOT_TOKEN")
    chat_id = _dashboard_telegram_setting("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    now = time.monotonic()
    last_sent = DASHBOARD_TELEGRAM_LAST_SENT.get(event_type)
    if last_sent and now - last_sent < TELEGRAM_EVENT_COOLDOWN_SECONDS:
        return False
    DASHBOARD_TELEGRAM_LAST_SENT[event_type] = now

    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
            "disable_notification": "true" if disable_notification else "false",
        }
    ).encode("utf-8")
    try:
        urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=10).read()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Dashboard Telegram notification failed: %s", _dashboard_safe_error(exc, token))
        return False


def _dashboard_telegram_enabled() -> bool:
    return _coerce_bool(_dashboard_telegram_setting("TELEGRAM_ENABLED"), False)


def _dashboard_telegram_setting(key: str) -> str | None:
    value = os.getenv(key)
    if value is not None:
        return value
    return _read_env_value(_bot_root("domain") / ".env", key)


def _dashboard_safe_error(error: Exception | str, *secrets: str | None) -> str:
    message = str(error)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


EMBEDDED_BOTS = {
    "yield": {
        "name": "Yield Optimizer",
        "root": BOTMASTER_ROOT / "project1" / "yield_optimizer_bot",
    },
    "domain": {
        "name": "Domain Hunter",
        "root": BOTMASTER_ROOT / "project2" / "projeto2",
    },
    "asset": {
        "name": "Asset Flip",
        "root": BOTMASTER_ROOT / "project3" / "asset_flip_bot",
    },
    "trend": {
        "name": "Trend Hunter",
        "root": BOTMASTER_ROOT / "project4" / "trend_hunter_bot",
    },
}

_EMBEDDED_BOTS_STARTED = False
_EMBEDDED_BOT_THREADS: list[threading.Thread] = []
_EMBEDDED_BOT_PROCESSES: dict[str, subprocess.Popen] = {}
_EMBEDDED_BOT_LOCK = threading.Lock()
_EMBEDDED_STOP_EVENT = threading.Event()
_EMBEDDED_DEPLOYMENT_LOCK_HANDLE: Any | None = None


def _embedded_bots_enabled() -> bool:
    value = os.getenv("BOTMASTER_EMBEDDED_BOTS", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _acquire_embedded_deployment_lock() -> bool:
    if os.name == "nt":
        return True
    try:
        import fcntl  # type: ignore
    except ImportError:
        return True

    global _EMBEDDED_DEPLOYMENT_LOCK_HANDLE
    lock_path = _shared_db_path().with_name("botmaster_embedded_bots.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    handle.write(str(os.getpid()))
    handle.flush()
    _EMBEDDED_DEPLOYMENT_LOCK_HANDLE = handle
    return True


def _embedded_bot_env(bot_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env["BOT_ID"] = bot_id
    env["PYTHONUNBUFFERED"] = "1"
    env["BOTMASTER_SHARED_DB"] = str(_shared_db_path())
    env["BOTMASTER_DISABLE_HTTP_STATUS"] = "1"
    for key in (
        "BOTMASTER_STATUS_ENDPOINT",
        "BOTMASTER_STATUS_HOSTPORT",
        "BOTMASTER_DASHBOARD_URL",
        "DASHBOARD_PUBLIC_URL",
        "BOTMASTER_STATUS_TOKEN",
    ):
        env.pop(key, None)
    return env


def _bot_supervisor(bot_id: str, name: str, root: Path) -> None:
    if not root.exists():
        _write_shared_status({"bot_id": bot_id, "bot_name": name, "status": "ERROR", "error": f"Bot directory not found: {root}"})
        if bot_id == "domain":
            _dashboard_telegram_alert(
                "bot_offline_detection",
                "Bot offline detection\n"
                f"Bot: {name}\n"
                "Status: ERROR\n"
                f"Reason: Bot directory not found: {root}",
            )
        return

    heartbeat_seconds = max(15, int(os.getenv("BOTMASTER_HEARTBEAT_SECONDS", "30")))
    restart_count = 0
    while not _EMBEDDED_STOP_EVENT.is_set():
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.main", "scheduler"],
                cwd=root,
                env=_embedded_bot_env(bot_id),
            )
            with _EMBEDDED_BOT_LOCK:
                _EMBEDDED_BOT_PROCESSES[bot_id] = process

            _write_shared_status(
                {
                    "bot_id": bot_id,
                    "bot_name": name,
                    "status": "RUNNING",
                    "metrics": {
                        "embedded": True,
                        "supervisor": "dashboard.app",
                        "pid": process.pid,
                        "restart_count": restart_count,
                        "root": str(root),
                    },
                }
            )
            print(f"[botmaster] Started {name} pid={process.pid} restart_count={restart_count}", flush=True)
            next_heartbeat = time.monotonic() + heartbeat_seconds

            while process.poll() is None and not _EMBEDDED_STOP_EVENT.is_set():
                now = time.monotonic()
                if now >= next_heartbeat:
                    _write_shared_status(
                        {
                            "bot_id": bot_id,
                            "bot_name": name,
                            "status": "RUNNING",
                            "metrics": {
                                "embedded": True,
                                "supervisor": "dashboard.app",
                                "pid": process.pid,
                                "restart_count": restart_count,
                                "heartbeat_seconds": heartbeat_seconds,
                            },
                        }
                    )
                    next_heartbeat = now + heartbeat_seconds
                time.sleep(2)

            if _EMBEDDED_STOP_EVENT.is_set() and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

            exit_code = process.poll()
            if _EMBEDDED_STOP_EVENT.is_set():
                _write_shared_status({"bot_id": bot_id, "bot_name": name, "status": "STOPPED"})
                break

            restart_count += 1
            error = f"Bot process exited with code {exit_code}; restarting"
            print(f"[botmaster] {name} {error}", flush=True)
            if bot_id == "domain":
                _dashboard_telegram_alert(
                    "bot_offline_detection",
                    "Bot offline detection\n"
                    f"Bot: {name}\n"
                    f"Status: ERROR\n"
                    f"Reason: {error}",
                )
            _write_shared_status(
                {
                    "bot_id": bot_id,
                    "bot_name": name,
                    "status": "ERROR",
                    "metrics": {
                        "embedded": True,
                        "supervisor": "dashboard.app",
                        "restart_count": restart_count,
                    },
                    "error": error,
                }
            )
            time.sleep(min(60, 5 * restart_count))
        except Exception as exc:  # noqa: BLE001
            restart_count += 1
            print(f"[botmaster] {name} supervisor error: {exc}", flush=True)
            if bot_id == "domain":
                _dashboard_telegram_alert(
                    "critical_exception",
                    "Critical exception\n"
                    f"Bot: {name}\n"
                    f"Error: {exc}",
                )
            _write_shared_status(
                {
                    "bot_id": bot_id,
                    "bot_name": name,
                    "status": "ERROR",
                    "metrics": {
                        "embedded": True,
                        "supervisor": "dashboard.app",
                        "restart_count": restart_count,
                    },
                    "error": str(exc),
                }
            )
            time.sleep(min(60, 5 * restart_count))
        finally:
            with _EMBEDDED_BOT_LOCK:
                if _EMBEDDED_BOT_PROCESSES.get(bot_id) is process:
                    _EMBEDDED_BOT_PROCESSES.pop(bot_id, None)

def _start_embedded_bots_once() -> None:
    global _EMBEDDED_BOTS_STARTED
    if not _embedded_bots_enabled():
        return
    with _EMBEDDED_BOT_LOCK:
        if _EMBEDDED_BOTS_STARTED:
            return
        if not _acquire_embedded_deployment_lock():
            return
        _EMBEDDED_BOTS_STARTED = True
        for bot_id, config in EMBEDDED_BOTS.items():
            thread = threading.Thread(
                target=_bot_supervisor,
                args=(bot_id, str(config["name"]), Path(config["root"])),
                name=f"botmaster-{bot_id}",
                daemon=True,
            )
            thread.start()
            _EMBEDDED_BOT_THREADS.append(thread)
        atexit.register(_stop_embedded_bots)


def _stop_embedded_bots() -> None:
    _EMBEDDED_STOP_EVENT.set()
    with _EMBEDDED_BOT_LOCK:
        processes = list(_EMBEDDED_BOT_PROCESSES.values())
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


@app.template_filter("json_script")
def json_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


_start_embedded_bots_once()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)






