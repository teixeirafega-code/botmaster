from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "shortmaster-scheduled.yml"
ROOT_RENDER_PATH = REPOSITORY_ROOT / "render.yaml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.config import ROOT_DIR, load_config, resolve_storage_path
from app.services.pipeline import ShortsMasterPipeline
from app.services.youtube_publish_scheduler import YouTubePublishScheduler


def main() -> None:
    config = load_config(ROOT_DIR)
    pipeline = ShortsMasterPipeline(config)
    publisher = YouTubePublishScheduler(pipeline, config)
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8") if WORKFLOW_PATH.exists() else ""
    root_render = load_yaml(ROOT_RENDER_PATH)
    crons = re.findall(r'cron:\s*["\']([^"\']+)["\']', workflow_text)
    render_workers = [
        service
        for service in root_render.get("services", [])
        if service.get("type") == "worker"
        and "shortmaster" in str(service.get("name", "")).lower()
    ]
    library = pipeline.background_library.scan()
    slots = publisher.upload_slots()
    now_local = datetime.now(ZoneInfo(config.get("app", {}).get("timezone", "UTC")))
    checks = {
        "workflow_exists": WORKFLOW_PATH.exists(),
        "five_daily_crons_configured": len(crons) == 5,
        "bounded_job_command_configured": "app.main scheduled-job" in workflow_text,
        "serialized_concurrency_configured": "shortmaster-scheduled-publishing" in workflow_text,
        "state_restore_configured": "actions/cache/restore@v4" in workflow_text,
        "state_save_configured": "actions/cache/save@v4" in workflow_text,
        "logs_and_reports_artifact_configured": "actions/upload-artifact@v4" in workflow_text,
        "private_upload_forced": "YOUTUBE_PRIVACY_STATUS: private" in workflow_text,
        "quality_threshold_75": 'MIN_UPLOAD_QUALITY_SCORE: "75"' in workflow_text,
        "safety_threshold_90": 'MIN_UPLOAD_SAFETY_SCORE: "90"' in workflow_text,
        "commercial_rights_gate_present": "background_commercial_rights_verified"
        in (PROJECT_ROOT / "app" / "services" / "safety.py").read_text(encoding="utf-8"),
        "render_shortmaster_worker_removed": not render_workers,
        "approved_commercial_background_available": bool(library["approved_assets"]),
    }
    blockers = [
        {"code": key.upper(), "detail": f"Required readiness check failed: {key}"}
        for key, passed in checks.items()
        if not passed
    ]
    configured_capacity = min(5, publisher.daily_upload_limit(), len(crons))
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "architecture": "github_actions_scheduled_ephemeral_jobs",
        "requires_24_7_worker": False,
        "render_shortmaster_worker_declared": bool(render_workers),
        "readiness_status": "configured" if not blockers else "blocked",
        "ready_for_scheduled_operation": not blockers,
        "checks": checks,
        "schedule": {
            "timezone": str(now_local.tzinfo),
            "cron_expressions_utc": crons,
            "local_upload_slots": slots,
            "jobs_per_day": len(crons),
            "next_10_scheduled_publish_times": next_publish_times(now_local, slots, 10),
        },
        "execution_contract": {
            "max_generated_per_run": 1,
            "max_uploaded_per_run": 1,
            "process_exits_after_run": True,
            "state_backend": "GitHub Actions cache for data/",
            "logs_backend": "GitHub Actions artifacts, 30-day retention",
            "configured_capacity_videos_per_day": configured_capacity,
        },
        "production_guardrails": {
            "quality_score_minimum": 75,
            "safety_score_minimum": 90,
            "commercial_background_rights_required": True,
            "privacy_status": "private",
            "automatic_approval_requires_all_gates": True,
        },
        "background_library": {
            "approved_commercial_background_count": len(library["approved_assets"]),
            "unapproved_or_unverified_background_count": max(
                0,
                len(library["assets"]) - len(library["approved_assets"]),
            ),
            "scan_warning_count": len(library["warnings"]),
        },
        "required_github_secrets": [
            "YOUTUBE_CHANNEL_ID",
            "YOUTUBE_CLIENT_SECRET_JSON",
            "YOUTUBE_TOKEN_JSON",
        ],
        "secret_verification_note": (
            "Secret values cannot be read locally. The workflow validates their presence before a production run."
        ),
        "blockers": blockers,
        "evidence": {
            "workflow": str(WORKFLOW_PATH),
            "root_render_manifest": str(ROOT_RENDER_PATH),
            "scheduled_job_service": str(
                PROJECT_ROOT / "app" / "services" / "scheduled_job.py"
            ),
        },
    }

    reports_dir = resolve_storage_path(
        config,
        config.get("storage", {}).get("reports_dir", "reports"),
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "publishing_readiness_report.json"
    html_path = reports_dir / "publishing_readiness_report.html"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=True, default=str),
        encoding="utf-8",
    )
    html_path.write_text(report_html(report), encoding="utf-8")
    print(
        json.dumps(
            {"json": str(json_path), "html": str(html_path), **report},
            indent=2,
            ensure_ascii=True,
        )
    )


def next_publish_times(
    now_local: datetime,
    slots: list[str],
    count: int,
) -> list[str]:
    candidates: list[datetime] = []
    day_offset = 0
    while len(candidates) < count:
        date = now_local.date() + timedelta(days=day_offset)
        for slot in slots:
            hour, minute = [int(part) for part in slot.split(":", 1)]
            candidate = datetime.combine(
                date,
                time(hour=hour, minute=minute),
                tzinfo=now_local.tzinfo,
            )
            if candidate > now_local:
                candidates.append(candidate)
                if len(candidates) == count:
                    break
        day_offset += 1
    return [item.isoformat() for item in candidates]


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def report_html(report: dict[str, Any]) -> str:
    rows = []
    for key, value in report.items():
        rendered = (
            json.dumps(value, indent=2, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
        )
        rows.append(
            f"<tr><th>{html.escape(str(key))}</th>"
            f"<td><pre>{html.escape(rendered)}</pre></td></tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>ShortMaster Publishing Readiness</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;"
        "padding:8px;text-align:left;vertical-align:top}th{width:280px;background:#f4f4f4}"
        "pre{white-space:pre-wrap;margin:0}</style></head><body>"
        "<h1>ShortMaster Publishing Readiness</h1>"
        f"<table>{''.join(rows)}</table></body></html>"
    )


if __name__ == "__main__":
    main()
