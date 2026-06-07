from __future__ import annotations

import html
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.publisher.youtube import YouTubePublisher
from app.services.config import ROOT_DIR, load_config, resolve_path


def main() -> None:
    config = load_config(ROOT_DIR)
    publisher_path = PROJECT_ROOT / "app" / "publisher" / "youtube.py"
    safety_path = PROJECT_ROOT / "app" / "services" / "safety.py"
    database_path = PROJECT_ROOT / "app" / "services" / "database.py"
    selector_path = PROJECT_ROOT / "app" / "services" / "topic_selector.py"
    pipeline_path = PROJECT_ROOT / "app" / "services" / "pipeline.py"
    requirements_path = PROJECT_ROOT / "requirements.txt"

    publisher_text = read_text(publisher_path)
    safety_text = read_text(safety_path)
    database_text = read_text(database_path)
    selector_text = read_text(selector_path)
    pipeline_text = read_text(pipeline_path)
    requirements_text = read_text(requirements_path)

    publishing = config.get("publishing", {})
    queue = config.get("queue", {})
    app = config.get("app", {})
    publisher = YouTubePublisher(config)

    client_secrets_path = resolve_path(config, publishing.get("youtube_client_secrets_file", ""))
    token_path = resolve_path(config, publishing.get("youtube_token_file", ""))
    client_secret_inspection = inspect_client_secret(publisher)

    live_upload_env = os.getenv("LIVE_UPLOAD_ENABLED")
    enable_real_upload_env = os.getenv("ENABLE_REAL_UPLOAD")
    channel_id = first_config_or_env(
        publishing,
        ["channel_id", "youtube_channel_id"],
        ["YOUTUBE_CHANNEL_ID", "CHANNEL_ID"],
    )
    channel_name = first_config_or_env(
        publishing,
        ["channel_name", "youtube_channel_name"],
        ["YOUTUBE_CHANNEL_NAME", "CHANNEL_NAME"],
    )

    google_packages = {
        "google-api-python-client": package_available("googleapiclient"),
        "google-auth-oauthlib": package_available("google_auth_oauthlib"),
        "google-auth": package_available("google.auth"),
    }
    requirements_mentions_google = all(
        name in requirements_text
        for name in ["google-api-python-client", "google-auth-oauthlib", "google-auth"]
    )

    upload_code_exists = all(
        [
            publisher_path.exists(),
            "class YouTubePublisher" in publisher_text,
            "def publish" in publisher_text,
            "videos().insert" in publisher_text,
            "MediaFileUpload" in publisher_text,
        ]
    )
    oauth_code_exists = all(
        [
            "InstalledAppFlow" in publisher_text,
            "Credentials.from_authorized_user_file" in publisher_text,
            "youtube.upload" in publisher_text,
        ]
    )
    quota_limit_exists = all(
        [
            "daily_upload_limit" in safety_text,
            "max_daily_upload_limit" in safety_text,
            "count_real_uploads_today" in safety_text + database_text,
        ]
    )
    performance_guard_exists = "performance_guard" in safety_text and "recent_real_video_metrics" in safety_text
    api_retry_or_quota_accounting_exists = bool(re.search(r"quota|rate limit|HttpError|retry", publisher_text, re.I))
    duplicate_topic_exists = all(
        [
            "seen_topics" in database_text,
            "topic_key TEXT NOT NULL UNIQUE" in database_text,
            "not self.db.is_seen" in selector_text,
        ]
    )
    duplicate_script_exists = all(
        [
            "_local_script_duplicate" in safety_text,
            "SequenceMatcher" in safety_text,
            "script_similarity_block_threshold" in safety_text,
        ]
    )
    final_human_approval_required = bool(queue.get("manual_approval", True))
    auto_process_after_approval = bool(queue.get("auto_process_approved", True))
    per_video_live_approval_exists = "approved_for_live_upload" in database_text and "approve_for_live_upload" in database_text

    quota_accounting_exists = "youtube_quota_usage" in database_text and "record_youtube_quota_usage" in database_text

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "did_publish_anything": False,
        "youtube_upload_code": {
            "exists": upload_code_exists,
            "publisher_file": str(publisher_path),
            "details": [
                "YouTubePublisher.publish exists" if "def publish" in publisher_text else "publish method missing",
                "Uses googleapiclient MediaFileUpload" if "MediaFileUpload" in publisher_text else "MediaFileUpload missing",
                "Calls videos().insert" if "videos().insert" in publisher_text else "videos().insert missing",
                "Simulates paper uploads when real upload disabled"
                if "Real upload disabled; simulating YouTube publish" in publisher_text
                else "paper upload simulation not found",
            ],
        },
        "oauth_and_credentials": {
            "oauth_code_exists": oauth_code_exists,
            "oauth_mode": publishing.get("oauth_mode", "local_server"),
            "scopes": list(YouTubePublisher.SCOPES),
            "client_secrets_file_configured": bool(publishing.get("youtube_client_secrets_file")),
            "client_secrets_file_path": str(client_secrets_path),
            "client_secrets_file_exists": client_secrets_path.exists(),
            "client_secret_inspection": client_secret_inspection,
            "token_file_configured": bool(publishing.get("youtube_token_file")),
            "token_file_path": str(token_path),
            "token_file_exists": token_path.exists(),
            "google_packages_installed": google_packages,
            "requirements_include_google_packages": requirements_mentions_google,
            "ready_for_oauth_without_user_action": client_secrets_path.exists() and token_path.exists(),
        },
        "mode_flags": {
            "paper_mode_current_value": bool(app.get("paper_mode", True)),
            "enable_real_upload_current_value": bool(publishing.get("enable_real_upload", False)),
            "live_upload_enabled_current_value": bool(publishing.get("live_upload_enabled", False)),
            "publisher_real_upload_enabled_effective": bool(publisher.real_upload_enabled),
            "LIVE_UPLOAD_ENABLED_env_exists": live_upload_env is not None,
            "LIVE_UPLOAD_ENABLED_current_value": live_upload_env,
            "ENABLE_REAL_UPLOAD_env_exists": enable_real_upload_env is not None,
            "ENABLE_REAL_UPLOAD_current_value": enable_real_upload_env,
        },
        "channel": {
            "channel_id_configured": bool(channel_id),
            "channel_id": channel_id,
            "channel_name_configured": bool(channel_name),
            "channel_name": channel_name,
        },
        "quota_and_rate_limits": {
            "daily_upload_limit_implemented": quota_limit_exists,
            "daily_upload_limit": int(publishing.get("daily_upload_limit", 1)),
            "max_daily_upload_limit": int(publishing.get("max_daily_upload_limit", 5)),
            "youtube_daily_quota_units": int(publishing.get("youtube_daily_quota_units", 10000)),
            "youtube_upload_quota_units": int(publishing.get("youtube_upload_quota_units", 1600)),
            "performance_guard_implemented": performance_guard_exists,
            "youtube_api_quota_accounting_implemented": quota_accounting_exists,
            "youtube_api_retry_or_rate_limit_handling_detected": api_retry_or_quota_accounting_exists,
            "notes": [
                "SafetyGuard blocks real uploads after the configured daily cap.",
                "YouTube upload quota unit accounting is implemented."
                if quota_accounting_exists
                else "No explicit YouTube Data API quota unit accounting was found.",
                "Publisher upload loop does not appear to handle googleapiclient HttpError rate-limit responses explicitly.",
            ],
        },
        "duplicate_upload_protection": {
            "topic_history_protection_exists": duplicate_topic_exists,
            "script_similarity_protection_exists": duplicate_script_exists,
            "details": [
                "seen_topics prevents selecting previously seen topic keys",
                "content_queue has a unique topic_key",
                "SafetyGuard checks recent script similarity before publish",
            ],
        },
        "human_approval": {
            "manual_approval_required_before_queue_processing": final_human_approval_required,
            "auto_process_approved_items": auto_process_after_approval,
            "per_video_live_approval_flag_exists": per_video_live_approval_exists,
            "final_human_approval_required_before_live_upload": final_human_approval_required
            and per_video_live_approval_exists,
            "notes": [
                "Manual approval is required to move pending topics into approved status.",
                "A separate approved_for_live_upload flag is required before real upload."
                if per_video_live_approval_exists
                else "No separate approved_for_live_upload flag was found.",
                "Once approved, auto_process_approved can generate and simulate/publish automatically.",
            ],
        },
        "readiness_summary": {},
    }
    report["readiness_summary"] = readiness_summary(report)

    output_dir = PROJECT_ROOT / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "youtube_upload_readiness_report.json"
    html_path = output_dir / "youtube_upload_readiness_report.html"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")

    print(json.dumps({"report_json": str(json_path), "report_html": str(html_path), **report}, indent=2, ensure_ascii=True))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def package_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def inspect_client_secret(publisher: YouTubePublisher) -> dict[str, Any]:
    try:
        return {"ok": True, **publisher.inspect_oauth_client_secret()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def first_config_or_env(config_section: dict[str, Any], config_keys: list[str], env_keys: list[str]) -> str | None:
    for key in config_keys:
        value = config_section.get(key)
        if value:
            return str(value)
    for key in env_keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def readiness_summary(report: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    if not report["youtube_upload_code"]["exists"]:
        blockers.append("YouTube upload code is incomplete or missing")
    if report["mode_flags"]["paper_mode_current_value"]:
        blockers.append("PAPER_MODE is true")
    if not report["mode_flags"]["enable_real_upload_current_value"]:
        blockers.append("publishing.enable_real_upload is false")
    if not report["mode_flags"]["live_upload_enabled_current_value"]:
        blockers.append("publishing.live_upload_enabled is false")
    if not report["oauth_and_credentials"]["client_secrets_file_exists"]:
        blockers.append("YouTube OAuth client secrets file is missing")
    elif not report["oauth_and_credentials"]["client_secret_inspection"].get("ok"):
        blockers.append("YouTube OAuth client secrets file is not a supported Desktop App credential")
    if not report["oauth_and_credentials"]["token_file_exists"]:
        warnings.append("YouTube OAuth token file is missing; first live run would require OAuth authorization")
    if not report["channel"]["channel_id_configured"] and not report["channel"]["channel_name_configured"]:
        warnings.append("No channel ID or channel name is configured")
    if not report["quota_and_rate_limits"]["youtube_api_quota_accounting_implemented"]:
        warnings.append("No YouTube API quota unit accounting is implemented")
    if not report["human_approval"]["manual_approval_required_before_queue_processing"]:
        blockers.append("Manual approval is not required")
    return {
        "live_upload_ready_now": not blockers,
        "safe_from_accidental_live_upload_now": (
            report["mode_flags"]["paper_mode_current_value"]
            or not report["mode_flags"]["publisher_real_upload_enabled_effective"]
        ),
        "blockers": blockers,
        "warnings": warnings,
    }


def render_html(report: dict[str, Any]) -> str:
    sections = []
    for section, value in report.items():
        if section == "project_root":
            continue
        sections.append(f"<h2>{html.escape(section)}</h2><pre>{html.escape(json.dumps(value, indent=2, ensure_ascii=True))}</pre>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>YouTube Upload Readiness Report</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
        "pre{background:#111;color:#eee;padding:16px;overflow:auto;border-radius:6px}"
        "h1,h2{color:#222}</style></head><body>"
        "<h1>YouTube Upload Readiness Report</h1>"
        f"<p><strong>Project:</strong> {html.escape(report['project_root'])}</p>"
        f"{''.join(sections)}</body></html>"
    )


if __name__ == "__main__":
    main()
