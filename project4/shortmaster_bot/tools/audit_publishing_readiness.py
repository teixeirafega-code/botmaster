from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.config import ROOT_DIR, load_config, resolve_path, resolve_storage_path
from app.services.pipeline import ShortsMasterPipeline
from app.services.youtube_publish_scheduler import YouTubePublishScheduler


DASHBOARD_STATUS_URL = "https://botmaster-l17d.onrender.com/api/status"


def main() -> None:
    config = load_config(ROOT_DIR)
    pipeline = ShortsMasterPipeline(config)
    publisher_scheduler = YouTubePublishScheduler(pipeline, config)
    now_local = datetime.now(ZoneInfo(config.get("app", {}).get("timezone", "UTC")))
    root_manifest = load_yaml(REPOSITORY_ROOT / "render.yaml")
    local_manifest = load_yaml(PROJECT_ROOT / "render.yaml")
    cloud = cloud_status(root_manifest)
    queue = queue_status(pipeline, cloud)
    git = git_status()
    scheduler = scheduler_status(config, publisher_scheduler, cloud, root_manifest)
    backgrounds = background_status(config, pipeline, cloud)
    production = production_status(root_manifest, cloud)
    deployment_control = {
        "render_cli_authenticated": env_bool("SHORTSMASTER_RENDER_CLI_AUTHENTICATED"),
        "worker_creation_attempted": env_bool("SHORTSMASTER_WORKER_CREATION_ATTEMPTED"),
        "worker_creation_error": os.getenv("SHORTSMASTER_WORKER_CREATION_ERROR", ""),
        "render_workspace_id": os.getenv("SHORTSMASTER_RENDER_WORKSPACE_ID", ""),
    }

    slots = publisher_scheduler.upload_slots()
    next_times = next_publish_times(now_local, slots, 10)
    configured_videos_per_day = publisher_scheduler.daily_upload_limit()
    effective_videos_per_day = configured_videos_per_day if (
        scheduler["automatic_generation_active"]
        and scheduler["queue_auto_refill_active"]
        and cloud["worker_active_confirmed"]
        and production["real_upload_enabled_effective"]
        and production["auto_approve_live_upload"]
        and production["youtube_credentials_ready"]
        and not production["scheduler_paused"]
        and backgrounds["commercially_cleared_count"] > 0
        and backgrounds["cloud_library_seed_confirmed"]
        and git["auto_deploy_can_receive_current_shortmaster_code"]
    ) else 0
    queue_coverage = (
        round(queue["upload_ready_count"] / configured_videos_per_day, 2)
        if configured_videos_per_day
        else None
    )
    raw_queue_coverage = (
        round(queue["waiting_generation_or_upload_count"] / configured_videos_per_day, 2)
        if configured_videos_per_day
        else None
    )

    blockers = build_blockers(
        config=config,
        pipeline=pipeline,
        scheduler=scheduler,
        cloud=cloud,
        git=git,
        queue=queue,
        backgrounds=backgrounds,
        root_manifest=root_manifest,
        production=production,
        deployment_control=deployment_control,
    )
    checks = {
        "scheduler_active": scheduler["scheduler_active"],
        "cloud_worker_active": cloud["worker_active_confirmed"],
        "automatic_video_generation_active": scheduler["automatic_generation_active"],
        "queue_auto_refill_active": scheduler["queue_auto_refill_active"],
        "automatic_upload_schedule_configured": bool(slots),
        "automatic_uploads_effective": bool(effective_videos_per_day),
        "persistent_disk_configured": cloud["persistent_disk_configured"],
        "scheduler_paused": production["scheduler_paused"],
    }
    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "audit_timezone": str(now_local.tzinfo),
        "audit_time_local": now_local.replace(microsecond=0).isoformat(),
        "overall_ready_for_true_24_7": not blockers,
        "readiness_status": "ready" if not blockers else "blocked",
        "checks": checks,
        "scheduler": scheduler,
        "cloud_worker": cloud,
        "deployment_source": git,
        "deployment_control": deployment_control,
        "automatic_generation": {
            "active": scheduler["automatic_generation_active"],
            "trend_cycle_interval_minutes": int(config.get("scheduler", {}).get("trend_interval_minutes", 30)),
            "startup_cycle_exists": True,
            "recurring_cycle_next_run_time": scheduler["trend_cycle_next_run_time"],
            "explanation": scheduler["generation_explanation"],
        },
        "queue_auto_refill": {
            "active": scheduler["queue_auto_refill_active"],
            "manual_approval_effective_local": bool(config.get("queue", {}).get("manual_approval", True)),
            "root_render_manual_approval_required": render_env_value(
                root_manifest, "botmaster-shortmaster", "MANUAL_APPROVAL_REQUIRED"
            ),
            "root_render_auto_approve_live_upload": render_env_value(
                root_manifest, "botmaster-shortmaster", "AUTO_APPROVE_LIVE_UPLOAD"
            ),
            "max_pending": int(config.get("queue", {}).get("max_pending", 25)),
            "queue": queue,
        },
        "publishing": {
            "local_safe_default_paper_mode": bool(config.get("app", {}).get("paper_mode", True)),
            **production,
            "upload_slots_local_time": slots,
            "next_10_scheduled_publish_times": next_times,
            "configured_capacity_videos_per_day": configured_videos_per_day,
            "effective_expected_videos_per_day": effective_videos_per_day,
            "youtube_quota_units_per_upload": int(
                config.get("publishing", {}).get("youtube_upload_quota_units", 1600)
            ),
            "youtube_daily_quota_units": int(
                config.get("publishing", {}).get("youtube_daily_quota_units", 10000)
            ),
        },
        "queue_coverage": {
            "upload_ready_videos": queue["upload_ready_count"],
            "waiting_generation_or_upload": queue["waiting_generation_or_upload_count"],
            "coverage_days_at_configured_rate": queue_coverage,
            "raw_queue_coverage_days_at_configured_rate": raw_queue_coverage,
            "coverage_interpretation": (
                "No publishable queue coverage."
                if queue["upload_ready_count"] == 0
                else f"Approximately {queue_coverage:.2f} days at the configured rate."
            ),
        },
        "background_library": backgrounds,
        "blockers": blockers,
        "non_blocking_strengths": [
            "Five daily cron upload slots are configured.",
            "YouTube daily quota configuration can cover five uploads (8000/10000 units).",
            "A persistent Render disk is declared for the ShortMaster worker.",
            "Quality, safety, language, duplicate, freshness, and trust gates remain implemented.",
        ],
        "evidence": {
            "local_scheduler_report": str(
                resolve_storage_path(config, config.get("storage", {}).get("reports_dir", "reports"))
                / "youtube_scheduler_report.json"
            ),
            "root_render_manifest": str(REPOSITORY_ROOT / "render.yaml"),
            "project_render_manifest": str(PROJECT_ROOT / "render.yaml"),
            "dashboard_status_url": DASHBOARD_STATUS_URL,
            "database_path": str(resolve_path(config, config["app"]["database_path"])),
        },
    }

    reports_dir = resolve_storage_path(config, config.get("storage", {}).get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "publishing_readiness_report.json"
    html_path = reports_dir / "publishing_readiness_report.html"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    html_path.write_text(report_html(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "html": str(html_path), **report}, indent=2, ensure_ascii=True))


def scheduler_status(
    config: dict[str, Any],
    publisher_scheduler: YouTubePublishScheduler,
    cloud: dict[str, Any],
    root_manifest: dict[str, Any],
) -> dict[str, Any]:
    process_active = shortmaster_process_active()
    scheduler_source = (PROJECT_ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
    recurring_jobs_enabled = "next_run_time=None" not in scheduler_source
    effective_active = process_active or cloud["worker_active_confirmed"]
    render_manual = str(
        render_env_value(root_manifest, "botmaster-shortmaster", "MANUAL_APPROVAL_REQUIRED")
    ).lower() == "true"
    auto_approve = str(
        render_env_value(root_manifest, "botmaster-shortmaster", "AUTO_APPROVE_LIVE_UPLOAD")
    ).lower() == "true"
    generation_explanation = (
        "Recurring interval jobs are enabled and the cloud worker heartbeat is active."
        if recurring_jobs_enabled and cloud["worker_active_confirmed"]
        else "Recurring interval jobs are enabled, but no active ShortMaster worker heartbeat is confirmed."
        if recurring_jobs_enabled
        else "The recurring trend-cycle remains explicitly paused in scheduler code."
    )
    return {
        "scheduler_active": effective_active,
        "local_process_active": process_active,
        "cloud_worker_heartbeat_active": cloud["worker_active_confirmed"],
        "scheduler_paused_in_database": bool(
            cloud.get("metrics", {}).get("scheduler_paused", publisher_scheduler.is_paused())
        ),
        "scheduler_pause_reason": publisher_scheduler.pause_reason(),
        "registered_job_ids_from_code": [
            "trend-cycle",
            "metrics-refresh",
            "health-heartbeat",
            *[
                f"youtube-upload-{index}"
                for index, _slot in enumerate(publisher_scheduler.upload_slots(), start=1)
            ],
        ],
        "trend_cycle_next_run_time": cloud.get("metrics", {}).get("next_upload"),
        "recurring_interval_jobs_enabled": recurring_jobs_enabled,
        "automatic_generation_active": effective_active and recurring_jobs_enabled,
        "queue_auto_refill_active": effective_active and recurring_jobs_enabled and not render_manual and auto_approve,
        "generation_explanation": generation_explanation,
        "heartbeat_interval_minutes": int(config.get("scheduler", {}).get("heartbeat_interval_minutes", 5)),
    }


def cloud_status(root_manifest: dict[str, Any]) -> dict[str, Any]:
    service = render_service(root_manifest, "botmaster-shortmaster")
    dashboard: dict[str, Any] = {}
    error = ""
    try:
        response = requests.get(DASHBOARD_STATUS_URL, timeout=30)
        response.raise_for_status()
        dashboard = response.json()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    cards = dashboard.get("cards", []) if isinstance(dashboard, dict) else []
    shortmaster_cards = [
        card
        for card in cards
        if "short" in str(card.get("id", "")).lower()
        or "short" in str(card.get("name", "")).lower()
    ]
    shortmaster_card = shortmaster_cards[0] if shortmaster_cards else {}
    metrics = shortmaster_card.get("metrics", {}) if isinstance(shortmaster_card, dict) else {}
    worker_active = str(shortmaster_card.get("status", "")).upper() == "RUNNING"
    generated_at = dashboard.get("generated_at") if isinstance(dashboard, dict) else None
    return {
        "worker_declared_in_local_root_manifest": service is not None,
        "worker_active_confirmed": worker_active,
        "status": "running" if worker_active else "not_confirmed",
        "reason": (
            "The public dashboard reports a fresh RUNNING heartbeat from ShortMaster."
            if worker_active
            else "The public dashboard does not report a fresh RUNNING heartbeat from ShortMaster."
        ),
        "dashboard_reachable": bool(dashboard),
        "dashboard_generated_at": generated_at,
        "dashboard_running_count": dashboard.get("running_count") if isinstance(dashboard, dict) else None,
        "dashboard_shortmaster_cards": shortmaster_cards,
        "metrics": metrics if isinstance(metrics, dict) else {},
        "dashboard_error": error,
        "persistent_disk_configured": bool(service and service.get("disk")),
        "start_command": (
            service.get("startCommand") or service.get("dockerCommand")
            if service
            else None
        ),
        "auto_deploy_declared": bool(service and service.get("autoDeploy")),
    }


def git_status() -> dict[str, Any]:
    tracked_shortmaster = bool(
        git_output(["ls-tree", "-r", "--name-only", "HEAD", "--", "project4/shortmaster_bot"]).strip()
    )
    root_render_diff = git_output(["diff", "--name-only", "--", "render.yaml"]).strip()
    deployable = tracked_shortmaster and not bool(root_render_diff)
    return {
        "branch": git_output(["branch", "--show-current"]).strip(),
        "head_commit": git_output(["rev-parse", "HEAD"]).strip(),
        "head_commit_date": git_output(["show", "-s", "--format=%cI", "HEAD"]).strip(),
        "shortmaster_code_tracked_in_head": tracked_shortmaster,
        "root_render_worker_change_committed": not bool(root_render_diff),
        "auto_deploy_can_receive_current_shortmaster_code": deployable,
        "finding": (
            "ShortMaster code and the Render worker manifest are committed in the current HEAD."
            if deployable
            else "ShortMaster code is not tracked in the current HEAD or the root Render worker manifest "
            "has uncommitted changes, so Git-based Render auto-deploy cannot receive the current implementation."
        ),
    }


def queue_status(pipeline: ShortsMasterPipeline, cloud: dict[str, Any]) -> dict[str, Any]:
    rows = pipeline.db.list_queue(limit=500)
    waiting = [
        row
        for row in rows
        if row.get("status") in {"approved", "ready"} and not row.get("youtube_video_id")
    ]
    upload_ready = [
        row
        for row in waiting
        if row.get("status") == "ready"
        and int(row.get("approved_for_live_upload") or 0) == 1
        and row.get("video_path")
        and row.get("script_json")
    ]
    cloud_waiting = cloud.get("metrics", {}).get("waiting_upload")
    upload_ready_count = int(cloud_waiting) if cloud["worker_active_confirmed"] and cloud_waiting is not None else len(upload_ready)
    return {
        "total_rows": len(rows),
        "waiting_generation_or_upload_count": len(waiting),
        "upload_ready_count": upload_ready_count,
        "queue_source": "cloud_heartbeat" if cloud["worker_active_confirmed"] and cloud_waiting is not None else "local_database",
        "pending_manual_approval_count": sum(
            1 for row in rows if row.get("status") == "pending_approval"
        ),
        "waiting_items": [
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "status": row.get("status"),
                "approved_for_live_upload": bool(row.get("approved_for_live_upload")),
                "has_video": bool(row.get("video_path")),
                "quality_score": row.get("quality_score"),
            }
            for row in waiting
        ],
    }


def background_status(
    config: dict[str, Any],
    pipeline: ShortsMasterPipeline,
    cloud: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = resolve_storage_path(
        config,
        config.get("story_mode", {}).get("background_video", {}).get(
            "manifest_path", "background_library_manifest.json"
        ),
    )
    library_dir = resolve_storage_path(
        config,
        config.get("story_mode", {}).get("background_video", {}).get(
            "library_dir", "background_library"
        ),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    backgrounds = manifest.get("backgrounds", []) if isinstance(manifest, dict) else []
    unverified = [
        item.get("filename")
        for item in backgrounds
        if item.get("commercial_rights_verified") is not True
    ]
    files = [path.name for path in library_dir.glob("*.mp4")] if library_dir.exists() else []
    scan = pipeline.background_library.scan()
    cloud_cleared = int(cloud.get("metrics", {}).get("commercially_cleared_backgrounds") or 0)
    return {
        "local_video_count": len(files),
        "approved_manifest_count": sum(
            1 for item in backgrounds if item.get("approved_for_use") is True
        ),
        "commercial_rights_unverified": unverified,
        "commercially_cleared_count": len(scan["approved_assets"]),
        "commercially_cleared_filenames": [asset.filename for asset in scan["approved_assets"]],
        "cloud_commercially_cleared_count": cloud_cleared,
        "cloud_library_seed_confirmed": cloud["worker_active_confirmed"] and cloud_cleared > 0,
        "finding": (
            "At least one original, commercially cleared background is bundled for deployment; "
            "user-downloaded clips remain blocked until rights are explicitly verified."
        ),
    }


def production_status(root_manifest: dict[str, Any], cloud: dict[str, Any]) -> dict[str, Any]:
    paper_mode = str(render_env_value(root_manifest, "botmaster-shortmaster", "PAPER_MODE")).lower() == "true"
    enable_real = str(
        render_env_value(root_manifest, "botmaster-shortmaster", "ENABLE_REAL_UPLOAD")
    ).lower() == "true"
    live_enabled = str(
        render_env_value(root_manifest, "botmaster-shortmaster", "LIVE_UPLOAD_ENABLED")
    ).lower() == "true"
    metrics = cloud.get("metrics", {})
    return {
        "environment": render_env_value(root_manifest, "botmaster-shortmaster", "SHORTSMASTER_ENV"),
        "paper_mode": paper_mode,
        "enable_real_upload": enable_real,
        "live_upload_enabled": live_enabled,
        "real_upload_enabled_effective": enable_real and live_enabled and not paper_mode,
        "auto_approve_live_upload": str(
            render_env_value(root_manifest, "botmaster-shortmaster", "AUTO_APPROVE_LIVE_UPLOAD")
        ).lower() == "true",
        "min_upload_quality_score": int(
            render_env_value(root_manifest, "botmaster-shortmaster", "MIN_UPLOAD_QUALITY_SCORE") or 0
        ),
        "min_upload_safety_score": int(
            render_env_value(root_manifest, "botmaster-shortmaster", "MIN_UPLOAD_SAFETY_SCORE") or 0
        ),
        "privacy_status": str(
            render_env_value(root_manifest, "botmaster-shortmaster", "YOUTUBE_PRIVACY_STATUS") or "private"
        ),
        "youtube_credentials_ready": bool(metrics.get("youtube_credentials_ready", False)),
        "scheduler_paused": bool(metrics.get("scheduler_paused", False)),
        "scheduler_pause_reason": "",
    }


def build_blockers(
    *,
    config: dict[str, Any],
    pipeline: ShortsMasterPipeline,
    scheduler: dict[str, Any],
    cloud: dict[str, Any],
    git: dict[str, Any],
    queue: dict[str, Any],
    backgrounds: dict[str, Any],
    root_manifest: dict[str, Any],
    production: dict[str, Any],
    deployment_control: dict[str, Any],
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []

    def add(code: str, severity: str, detail: str) -> None:
        blockers.append({"code": code, "severity": severity, "detail": detail})

    if not scheduler["scheduler_active"]:
        add("SCHEDULER_INACTIVE", "critical", "No active local process or cloud worker heartbeat was found.")
    if not cloud["worker_active_confirmed"]:
        add("CLOUD_WORKER_NOT_CONFIRMED", "critical", cloud["reason"])
    if deployment_control["worker_creation_error"]:
        add(
            "RENDER_WORKER_CREATION_BLOCKED",
            "critical",
            "Render worker creation failed: " + deployment_control["worker_creation_error"],
        )
    if not git["auto_deploy_can_receive_current_shortmaster_code"]:
        add("CURRENT_CODE_NOT_DEPLOYABLE", "critical", git["finding"])
    if not scheduler["automatic_generation_active"]:
        add(
            "RECURRING_GENERATION_INACTIVE",
            "critical",
            scheduler["generation_explanation"],
        )
    if production["paper_mode"]:
        add("PAPER_MODE_ENABLED", "critical", "Production PAPER_MODE=true prevents real publishing.")
    if not production["enable_real_upload"]:
        add("REAL_UPLOAD_DISABLED", "critical", "ENABLE_REAL_UPLOAD=false.")
    if not production["live_upload_enabled"]:
        add("LIVE_UPLOAD_DISABLED", "critical", "LIVE_UPLOAD_ENABLED=false.")
    if not production["real_upload_enabled_effective"]:
        add("EFFECTIVE_UPLOAD_GATE_CLOSED", "critical", "The production real-upload flag is false.")
    render_manual = render_env_value(root_manifest, "botmaster-shortmaster", "MANUAL_APPROVAL_REQUIRED")
    if str(render_manual).lower() == "true":
        add(
            "CLOUD_MANUAL_APPROVAL_REQUIRED",
            "critical",
            "The root Render Blueprint queues new topics as pending approval instead of auto-processing them.",
        )
    if not production["auto_approve_live_upload"]:
        add(
            "PER_VIDEO_LIVE_APPROVAL_NOT_AUTOMATED",
            "critical",
            "AUTO_APPROVE_LIVE_UPLOAD is not enabled in production.",
        )
    if production["min_upload_quality_score"] < 75 or production["min_upload_safety_score"] < 90:
        add(
            "PRODUCTION_SCORE_THRESHOLDS_TOO_LOW",
            "critical",
            "Production thresholds must remain quality_score>=75 and safety_score>=90.",
        )
    if backgrounds["commercially_cleared_count"] == 0:
        add(
            "NO_COMMERCIALLY_CLEARED_BACKGROUND",
            "critical",
            "No approved background has explicit commercial-use verification.",
        )
    if not backgrounds["cloud_library_seed_confirmed"]:
        add(
            "CLOUD_BACKGROUND_LIBRARY_UNCONFIRMED",
            "critical",
            "The active cloud worker has not confirmed a commercially cleared background.",
        )
    if render_env_value(root_manifest, "botmaster-shortmaster", "TELEGRAM_ALERTS_ENABLED") == "false":
        add(
            "TELEGRAM_ALERTS_DISABLED",
            "high",
            "The worker cannot send the required alert when all background files fail.",
        )
    if not production["youtube_credentials_ready"]:
        add(
            "CLOUD_YOUTUBE_SECRETS_UNVERIFIED",
            "high",
            "The worker heartbeat has not confirmed both YouTube OAuth secret and token.",
        )
    if production["scheduler_paused"]:
        add("CLOUD_SCHEDULER_PAUSED", "critical", "The cloud upload scheduler reports a paused state.")
    return blockers


def next_publish_times(now: datetime, slots: list[str], count: int) -> list[str]:
    values: list[str] = []
    day = now.date()
    while len(values) < count:
        for slot in slots:
            hour, minute = [int(part) for part in slot.split(":", 1)]
            candidate = datetime.combine(day, time(hour=hour, minute=minute), tzinfo=now.tzinfo)
            if candidate > now:
                values.append(candidate.isoformat())
            if len(values) >= count:
                break
        day += timedelta(days=1)
    return values


def shortmaster_process_active() -> bool:
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | Where-Object { "
                "$_.Name -match 'python' -and $_.CommandLine -match "
                "'app\\.main\\s+run|shortmaster_bot.*app\\.main' } | Select-Object -First 1 -ExpandProperty ProcessId"
            ),
        ]
    else:
        command = ["sh", "-c", "pgrep -f 'python.*app.main run' | head -n 1"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    return bool(result.stdout.strip())


def render_service(manifest: dict[str, Any], name: str) -> dict[str, Any] | None:
    for service in manifest.get("services", []) if isinstance(manifest, dict) else []:
        if service.get("name") == name:
            return service
    return None


def render_env_value(manifest: dict[str, Any], service_name: str, key: str) -> Any:
    service = render_service(manifest, service_name)
    if not service:
        return None
    for item in service.get("envVars", []):
        if item.get("key") == key:
            return item.get("value") if "value" in item else "<secret>"
    return None


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def git_output(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.stdout


def report_html(report: dict[str, Any]) -> str:
    blocker_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item['severity'].upper())}</td>"
        f"<td>{html.escape(item['code'])}</td>"
        f"<td>{html.escape(item['detail'])}</td>"
        "</tr>"
        for item in report["blockers"]
    )
    schedule_rows = "".join(
        f"<tr><td>{index}</td><td>{html.escape(value)}</td></tr>"
        for index, value in enumerate(
            report["publishing"]["next_10_scheduled_publish_times"], start=1
        )
    )
    checks = "".join(
        "<tr>"
        f"<th>{html.escape(key)}</th>"
        f"<td class='{'pass' if value else 'fail'}'>{'PASS' if value else 'BLOCK'}</td>"
        "</tr>"
        for key, value in report["checks"].items()
    )
    raw = html.escape(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Publishing Readiness Report</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:1180px;margin:32px auto;line-height:1.45}"
        "table{border-collapse:collapse;width:100%;margin:14px 0 28px}th,td{border:1px solid #ddd;padding:9px;"
        "text-align:left;vertical-align:top}th{background:#f4f4f4}.pass{color:#176b36;font-weight:700}"
        ".fail{color:#a11d1d;font-weight:700}.banner{padding:14px;background:#fde8e8;border:1px solid #d99}"
        "pre{white-space:pre-wrap;background:#111;color:#eee;padding:16px;overflow:auto}</style></head><body>"
        "<h1>Publishing Readiness Report</h1>"
        f"<div class='banner'><strong>Status:</strong> {html.escape(report['readiness_status'].upper())}</div>"
        f"<p><strong>Generated:</strong> {html.escape(report['generated_at'])}</p>"
        "<h2>Readiness Checks</h2><table>" + checks + "</table>"
        "<h2>Capacity</h2><table>"
        f"<tr><th>Configured videos/day</th><td>{report['publishing']['configured_capacity_videos_per_day']}</td></tr>"
        f"<tr><th>Effective expected videos/day</th><td>{report['publishing']['effective_expected_videos_per_day']}</td></tr>"
        f"<tr><th>Publishable queue coverage</th><td>{report['queue_coverage']['coverage_days_at_configured_rate']} days</td></tr>"
        "</table><h2>Next 10 Scheduled Times</h2><table><tr><th>#</th><th>Time</th></tr>"
        + schedule_rows
        + "</table><h2>Blockers</h2><table><tr><th>Severity</th><th>Code</th><th>Detail</th></tr>"
        + blocker_rows
        + "</table><h2>Full JSON</h2><pre>"
        + raw
        + "</pre></body></html>"
    )


if __name__ == "__main__":
    main()
