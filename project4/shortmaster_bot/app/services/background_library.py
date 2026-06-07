from __future__ import annotations

import html
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.alerts import TelegramAlertClient
from app.services.config import resolve_storage_path

try:
    from moviepy import VideoFileClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import VideoFileClip


LOGGER = logging.getLogger(__name__)


class BackgroundLibraryUnavailableError(RuntimeError):
    """Raised when no approved, playable custom background remains."""


@dataclass(frozen=True)
class BackgroundAsset:
    filename: str
    path: Path
    source_url: str
    license_type: str
    added_by_user: str
    approved_for_use: bool
    commercial_rights_verified: bool
    category: str


class BackgroundLibraryManager:
    def __init__(self, config: dict[str, Any], database=None, rng=None):
        self.config = config
        self.database = database
        background = config.get("story_mode", {}).get("background_video", {})
        self.library_dir = resolve_storage_path(config, background.get("library_dir", "background_library"))
        self.manifest_path = resolve_storage_path(
            config,
            background.get("manifest_path", "background_library_manifest.json"),
        )
        self.reports_dir = resolve_storage_path(
            config,
            config.get("storage", {}).get("reports_dir", "reports"),
        )
        self.alerts = TelegramAlertClient(config)
        self.rng = rng or random.SystemRandom()
        self.last_warnings: list[str] = []
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_manifest()

    def scan(self) -> dict[str, Any]:
        self.last_warnings = []
        manifest = self._load_manifest()
        entries = manifest.get("backgrounds", [])
        if not isinstance(entries, list):
            self.last_warnings.append("background_library_manifest.json must contain a backgrounds list")
            entries = []

        files = {
            path.name.lower(): path
            for path in self.library_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".mp4"
        }
        manifest_by_name: dict[str, dict[str, Any]] = {}
        for raw in entries:
            if not isinstance(raw, dict):
                self.last_warnings.append("Ignored invalid non-object background manifest entry")
                continue
            filename = str(raw.get("filename", "") or "").strip()
            if not filename or Path(filename).name != filename or Path(filename).suffix.lower() != ".mp4":
                self.last_warnings.append(f"Ignored unsafe or non-MP4 manifest filename: {filename or '<empty>'}")
                continue
            manifest_by_name[filename.lower()] = raw

        assets: list[BackgroundAsset] = []
        for key, path in sorted(files.items()):
            raw = manifest_by_name.get(key)
            if raw is None:
                self.last_warnings.append(f"{path.name} is not listed in background_library_manifest.json")
                continue
            source_url = str(raw.get("source_url", "") or "").strip()
            license_type = str(raw.get("license_type", "") or "").strip()
            added_by_user = str(raw.get("added_by_user", "") or "").strip()
            if not source_url or not license_type:
                self.last_warnings.append(f"{path.name} has missing license metadata")
            commercial_rights_verified = raw.get("commercial_rights_verified") is True
            if raw.get("approved_for_use") is True and not commercial_rights_verified:
                self.last_warnings.append(
                    f"{path.name} is approved_for_use but commercial rights are not verified; publishing is blocked"
                )
            assets.append(
                BackgroundAsset(
                    filename=path.name,
                    path=path.resolve(),
                    source_url=source_url,
                    license_type=license_type,
                    added_by_user=added_by_user,
                    approved_for_use=raw.get("approved_for_use") is True,
                    commercial_rights_verified=commercial_rights_verified,
                    category=str(raw.get("category", "custom_library") or "custom_library").strip(),
                )
            )

        approved_assets = [
            asset
            for asset in assets
            if asset.approved_for_use
            and asset.commercial_rights_verified
            and bool(asset.source_url)
            and bool(asset.license_type)
        ]
        result = {
            "files": [path.name for path in sorted(files.values(), key=lambda item: item.name.lower())],
            "assets": assets,
            "approved_assets": approved_assets,
            "approved_for_use_assets": [asset for asset in assets if asset.approved_for_use],
            "warnings": list(self.last_warnings),
        }
        for warning in result["warnings"]:
            LOGGER.warning("Background library: %s", warning)
        return result

    def select(self, excluded: set[str] | None = None) -> BackgroundAsset:
        excluded_names = {name.lower() for name in (excluded or set())}
        scan = self.scan()
        candidates = [
            asset
            for asset in scan["approved_assets"]
            if asset.filename.lower() not in excluded_names
        ]
        self.rng.shuffle(candidates)
        failures: list[str] = []
        for asset in candidates:
            try:
                self._probe_video(asset.path)
                LOGGER.info("Selected approved custom background: %s", asset.filename)
                return asset
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failures.append(f"{asset.filename}: {error}")
                self.record_failure(asset.filename, error)
                LOGGER.warning("Skipping failed custom background %s: %s", asset.filename, error)

        if not scan["approved_assets"]:
            reason = (
                "No approved MP4 backgrounds with verified commercial rights are available "
                "in background_library"
            )
        elif excluded_names and not candidates:
            reason = "All approved custom backgrounds failed during this render"
        else:
            reason = "All approved custom backgrounds failed validation"
        if failures:
            reason = f"{reason}: {'; '.join(failures)}"
        self.pause_publishing(reason)
        self.write_report()
        raise BackgroundLibraryUnavailableError(reason)

    def record_failure(self, filename: str, error: str) -> None:
        if self.database is not None:
            self.database.record_background_library_event(filename, "failure", error)

    def record_selected(self, filename: str) -> None:
        if self.database is not None:
            self.database.record_background_library_event(filename, "selected")

    def pause_publishing(self, reason: str) -> None:
        already_paused = False
        if self.database is not None:
            state = self.database.get_scheduler_state("youtube_upload_scheduler")
            already_paused = bool(state.get("paused")) and "background" in str(
                state.get("reason", "")
            ).lower()
            if not already_paused:
                self.database.set_scheduler_state(
                    "youtube_upload_scheduler",
                    {
                        "paused": True,
                        "reason": reason,
                        "paused_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    },
                )
                self.database.record_scheduler_event(
                    "background_library",
                    "paused",
                    reason=reason,
                )
        if not already_paused:
            self.alerts.send("background library unavailable", reason)
        LOGGER.error("Publishing paused because the custom background library is unavailable: %s", reason)

    def resume_publishing_if_library_available(self) -> bool:
        if self.database is None or not self.scan()["approved_assets"]:
            return False
        state = self.database.get_scheduler_state("youtube_upload_scheduler")
        reason = str(state.get("reason", "") or "").lower()
        if not state.get("paused") or not any(
            marker in reason
            for marker in ("background", "commercial rights", "approved mp4")
        ):
            return False
        self.database.set_scheduler_state(
            "youtube_upload_scheduler",
            {"paused": False, "reason": ""},
        )
        self.database.record_scheduler_event(
            "background_library",
            "resumed",
            reason="commercially cleared background is available",
        )
        LOGGER.info("Publishing resumed because a commercially cleared background is available")
        return True

    def write_report(self) -> dict[str, str]:
        scan = self.scan()
        performance = self.database.background_library_performance() if self.database is not None else []
        events = self.database.background_library_event_summary() if self.database is not None else []
        last_used = self.database.last_used_background() if self.database is not None else None
        event_by_name = {row["filename"]: row for row in events}
        performance_by_name = {row["filename"]: row for row in performance}
        names = sorted(
            set(scan["files"])
            | set(event_by_name)
            | set(performance_by_name),
            key=str.lower,
        )
        backgrounds = []
        for filename in names:
            event = event_by_name.get(filename, {})
            perf = performance_by_name.get(filename, {})
            backgrounds.append(
                {
                    "filename": filename,
                    "usage_count": max(
                        int(perf.get("video_count", 0) or 0),
                        int(event.get("selected_count", 0) or 0),
                    ),
                    "error_count": int(event.get("error_count", 0) or 0),
                    "avg_quality_score": perf.get("avg_quality_score"),
                    "avg_views": perf.get("avg_views"),
                    "avg_likes": perf.get("avg_likes"),
                    "avg_comments": perf.get("avg_comments"),
                    "avg_like_rate": perf.get("avg_like_rate"),
                    "youtube_stats_available": bool(perf.get("youtube_stats_available", False)),
                    "retention_data_available": False,
                    "avg_retention_seconds": None,
                }
            )
        failed_names = [row["filename"] for row in backgrounds if int(row["error_count"]) > 0]
        payload = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "library_directory": str(self.library_dir),
            "manifest_path": str(self.manifest_path),
            "total_backgrounds": len(scan["files"]),
            "approved_backgrounds": len(scan["approved_assets"]),
            "approved_for_use_backgrounds": len(scan["approved_for_use_assets"]),
            "commercially_cleared_backgrounds": len(scan["approved_assets"]),
            "failed_backgrounds": len(failed_names),
            "failed_background_filenames": failed_names,
            "last_used_background": last_used,
            "warnings": scan["warnings"],
            "backgrounds": backgrounds,
        }
        json_path = self.reports_dir / "background_library_report.json"
        html_path = self.reports_dir / "background_library_report.html"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        html_path.write_text(self._html_report(payload), encoding="utf-8")
        return {"json": str(json_path), "html": str(html_path)}

    def _ensure_manifest(self) -> None:
        if self.manifest_path.exists():
            return
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps({"version": 2, "backgrounds": []}, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def _load_manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_warnings.append(f"Could not read background library manifest: {exc}")
            return {"version": 2, "backgrounds": []}
        if not isinstance(payload, dict):
            self.last_warnings.append("Background library manifest root must be an object")
            return {"version": 2, "backgrounds": []}
        return payload

    def _probe_video(self, path: Path) -> None:
        clip = VideoFileClip(str(path), audio=False)
        try:
            if float(clip.duration or 0.0) <= 0:
                raise ValueError("video duration must be greater than zero")
            if int(clip.w or 0) <= 0 or int(clip.h or 0) <= 0:
                raise ValueError("video dimensions must be greater than zero")
        finally:
            clip.close()

    def _html_report(self, report: dict[str, Any]) -> str:
        rows = []
        for key, value in report.items():
            rendered = json.dumps(value, indent=2, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            rows.append(
                "<tr>"
                f"<th>{html.escape(str(key))}</th>"
                f"<td><pre>{html.escape(rendered)}</pre></td>"
                "</tr>"
            )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Background Library Report</title>"
            "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
            "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}"
            "th{width:280px;background:#f4f4f4}pre{white-space:pre-wrap;margin:0}</style></head>"
            f"<body><h1>Background Library Report</h1><table>{''.join(rows)}</table></body></html>"
        )
