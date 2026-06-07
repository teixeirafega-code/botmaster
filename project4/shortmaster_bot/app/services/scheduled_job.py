from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import QueueStatus
from app.services.botmaster_status import BotmasterStatusClient
from app.services.config import resolve_storage_path
from app.services.pipeline import ShortsMasterPipeline
from app.services.youtube_publish_scheduler import YouTubePublishScheduler


LOGGER = logging.getLogger(__name__)


class ScheduledPublishingJob:
    """Run one bounded generation/upload unit and then exit."""

    def __init__(self, pipeline: ShortsMasterPipeline, config: dict[str, Any]):
        self.pipeline = pipeline
        self.config = config
        self.publisher = YouTubePublishScheduler(pipeline, config)
        self.status = BotmasterStatusClient()

    def run(self, job_id: str = "") -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        effective_job_id = job_id or os.getenv("GITHUB_RUN_ID", "") or started_at
        report: dict[str, Any] = {
            "job_id": effective_job_id,
            "execution_mode": "scheduled_ephemeral_job",
            "started_at": started_at,
            "finished_at": None,
            "generated_count": 0,
            "upload_attempt_count": 0,
            "uploaded_count": 0,
            "generation_result": None,
            "upload_result": None,
            "metrics_refresh": {"attempted": False, "updated": 0, "error": ""},
            "paper_mode": bool(self.config.get("app", {}).get("paper_mode", True)),
            "real_upload_enabled": bool(self.pipeline.publisher.real_upload_enabled),
            "privacy_status": str(
                self.config.get("publishing", {}).get("privacy_status", "private")
            ),
            "quality_threshold": float(
                self.config.get("publishing", {}).get("min_upload_quality_score", 75)
            ),
            "safety_threshold": float(
                self.config.get("publishing", {}).get("min_upload_safety_score", 90)
            ),
            "commercial_rights_required": True,
            "status": "running",
            "error": "",
        }
        self._write_status("RUNNING", report)

        try:
            self.pipeline.background_library.resume_publishing_if_library_available()
            ready = self.pipeline.db.next_upload_candidate(
                max_attempts=int(
                    self.config.get("scheduler", {}).get(
                        "max_upload_attempts_per_video", 3
                    )
                )
            )
            if ready is None:
                report["generation_result"] = self._generate_one()
                report["generated_count"] = int(
                    bool(
                        report["generation_result"]
                        and report["generation_result"].get("generation_attempted")
                    )
                )

            report["upload_attempt_count"] = 1
            upload_result = self.publisher.publish_next_ready()
            report["upload_result"] = upload_result
            report["uploaded_count"] = int(bool(upload_result.get("uploaded")))
            report["status"] = self._result_status(upload_result)
            self._refresh_metrics(report)
        except Exception as exc:
            LOGGER.exception("Scheduled publishing job %s failed", effective_job_id)
            report["status"] = "failure"
            report["error"] = f"{type(exc).__name__}: {exc}"
            self.pipeline.db.record_scheduler_event(
                "scheduled_job",
                "failure",
                reason=report["error"],
                payload={"job_id": effective_job_id},
            )
        finally:
            report["finished_at"] = datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat()
            report["queue_waiting"] = self.pipeline.db.count_waiting_for_upload()
            report["uploads_today"] = self.pipeline.db.count_real_uploads_today()
            report["scheduler_paused"] = self.publisher.is_paused()
            report["scheduler_pause_reason"] = self.publisher.pause_reason()
            paths = self.write_report(report)
            report["artifacts"] = paths
            self.write_report(report)
            self._write_status(
                "ERROR" if report["status"] == "failure" else "IDLE",
                report,
                error=report["error"] or None,
            )
        return report

    def _generate_one(self) -> dict[str, Any]:
        max_attempts = int(self.config.get("scheduler", {}).get("generation_attempt_limit", 3))
        attempts: list[dict[str, Any]] = []
        existing_items = self.pipeline.db.list_for_processing(limit=max_attempts)
        for item in existing_items:
            result = self._process_generation_item(item)
            attempts.append(result)
            if result.get("upload_ready"):
                return self._generation_summary(attempts, result)

        for _ in range(max_attempts - len(attempts)):
            item = self.pipeline.discover_and_queue()
            if not item:
                break
            result = self._process_generation_item(item)
            attempts.append(result)
            if result.get("upload_ready"):
                return self._generation_summary(attempts, result)

        if attempts:
            return self._generation_summary(
                attempts,
                attempts[-1],
                reason="No generated item passed upload readiness gates",
            )
        return {
            "generation_attempted": False,
            "status": "empty",
            "reason": "No eligible unseen story was available",
            "attempts": [],
        }

    def _process_generation_item(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("status") == QueueStatus.PENDING_APPROVAL:
            return {
                "generation_attempted": False,
                "status": "blocked",
                "queue_id": item.get("id"),
                "reason": "Manual approval is enabled for an autonomous scheduled job",
                "upload_ready": False,
            }

        queue_id = int(item["id"])
        processed = self.pipeline.process_item(item, publish_after_generate=False)
        approved_for_live_upload = bool(
            int(processed.get("approved_for_live_upload") or 0) == 1
        )
        commercial_rights_verified = bool(
            int(processed.get("background_commercial_rights_verified") or 0) == 1
        )
        upload_ready = bool(
            processed.get("status") == QueueStatus.READY
            and approved_for_live_upload
            and processed.get("video_path")
            and commercial_rights_verified
        )
        return {
            "generation_attempted": True,
            "queue_id": queue_id,
            "status": processed.get("status"),
            "quality_score": processed.get("quality_score"),
            "approved_for_live_upload": approved_for_live_upload,
            "background_filename": processed.get("background_filename"),
            "background_commercial_rights_verified": commercial_rights_verified,
            "upload_blocked_reason": processed.get("upload_blocked_reason"),
            "upload_ready": upload_ready,
        }

    def _generation_summary(
        self,
        attempts: list[dict[str, Any]],
        selected: dict[str, Any],
        reason: str = "",
    ) -> dict[str, Any]:
        summary = dict(selected)
        summary["attempts"] = attempts
        summary["attempt_count"] = len(attempts)
        if reason and not summary.get("upload_ready"):
            summary["reason"] = reason
        return summary

    def _refresh_metrics(self, report: dict[str, Any]) -> None:
        report["metrics_refresh"]["attempted"] = True
        try:
            report["metrics_refresh"]["updated"] = self.pipeline.refresh_metrics()
        except Exception as exc:
            report["metrics_refresh"]["error"] = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("Metrics refresh failed after scheduled job: %s", exc)

    def _result_status(self, upload_result: dict[str, Any]) -> str:
        status = str(upload_result.get("status", "unknown"))
        if upload_result.get("uploaded"):
            return "success"
        if status in {"empty", "skipped"}:
            return status
        if status in {"paused", "failure", "validation_failed"}:
            return status
        return "completed_without_upload"

    def write_report(self, report: dict[str, Any]) -> dict[str, str]:
        reports_dir = resolve_storage_path(
            self.config,
            self.config.get("storage", {}).get("reports_dir", "reports"),
        )
        reports_dir.mkdir(parents=True, exist_ok=True)
        latest = reports_dir / "scheduled_job_report.json"
        run_report = reports_dir / f"scheduled_job_{report['job_id']}.json"
        payload = json.dumps(report, indent=2, ensure_ascii=True, default=str)
        latest.write_text(payload, encoding="utf-8")
        run_report.write_text(payload, encoding="utf-8")
        return {"latest_json": str(latest), "run_json": str(run_report)}

    def _write_status(
        self,
        status: str,
        report: dict[str, Any],
        error: str | None = None,
    ) -> None:
        self.status.write(
            status=status,
            metrics={
                "execution_mode": "scheduled_ephemeral_job",
                "job_id": report.get("job_id"),
                "generated_count": report.get("generated_count", 0),
                "uploaded_count": report.get("uploaded_count", 0),
                "uploads_today": report.get("uploads_today", 0),
                "privacy_status": report.get("privacy_status"),
                "quality_threshold": report.get("quality_threshold"),
                "safety_threshold": report.get("safety_threshold"),
                "commercial_rights_required": True,
            },
            error=error,
        )
