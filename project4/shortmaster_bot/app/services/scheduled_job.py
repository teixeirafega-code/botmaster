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
            "workflow_stages": self._empty_workflow_stages(),
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
                self._apply_generation_stages(report, report["generation_result"])
                ready = self.pipeline.db.next_upload_candidate(
                    max_attempts=int(
                        self.config.get("scheduler", {}).get(
                            "max_upload_attempts_per_video", 3
                        )
                    )
                )
            else:
                ready_video_path = self.pipeline.resolve_existing_video_path(ready)
                self._mark_stage(
                    report,
                    "story_selected",
                    True,
                    queue_id=ready.get("id"),
                    title=ready.get("title"),
                    source="existing_upload_candidate",
                )
                self._mark_stage(
                    report,
                    "video_rendered",
                    bool(ready_video_path and ready_video_path.exists()),
                    queue_id=ready.get("id"),
                    video_path_present=bool(ready_video_path and ready_video_path.exists()),
                    source="existing_upload_candidate",
                )
                self._mark_stage(
                    report,
                    "validation_passed",
                    bool(ready.get("status") == QueueStatus.READY and int(ready.get("approved_for_live_upload") or 0) == 1),
                    queue_id=ready.get("id"),
                    quality_score=ready.get("quality_score"),
                    approved_for_live_upload=bool(int(ready.get("approved_for_live_upload") or 0) == 1),
                    source="existing_upload_candidate",
                )
                self._mark_stage(
                    report,
                    "queued_ready",
                    True,
                    queue_id=ready.get("id"),
                    status=ready.get("status"),
                    source="existing_upload_candidate",
                )

            if ready is None:
                reason = "Generation did not produce an approved READY video for upload"
                report["upload_result"] = {
                    "status": "generation_failed",
                    "reason": reason,
                    "uploaded": False,
                }
                report["status"] = "generation_failed"
                self.pipeline.db.record_scheduler_event(
                    "scheduled_job",
                    "generation_failed",
                    reason=reason,
                    payload={
                        "job_id": effective_job_id,
                        "generation_result": report.get("generation_result"),
                    },
                )
                LOGGER.error("upload_attempted=false reason=%s", reason)
                self._refresh_metrics(report)
                return report

            report["upload_attempt_count"] = 1
            self._mark_stage(report, "upload_attempted", True, queue_id=ready.get("id"))
            upload_result = self.publisher.publish_next_ready()
            report["upload_result"] = upload_result
            report["uploaded_count"] = int(bool(upload_result.get("uploaded")))
            if upload_result.get("uploaded"):
                uploaded_item = self.pipeline.db.get_queue_item(int(upload_result.get("queue_id") or 0)) or {}
                uploaded_video_path = self.pipeline.resolve_existing_video_path(uploaded_item)
                self._mark_stage(
                    report,
                    "video_rendered",
                    True,
                    queue_id=upload_result.get("queue_id"),
                    video_path_present=bool(uploaded_video_path and uploaded_video_path.exists()),
                    source="uploaded",
                )
                self._mark_stage(
                    report,
                    "validation_passed",
                    True,
                    queue_id=upload_result.get("queue_id"),
                    quality_score=uploaded_item.get("quality_score"),
                    approved_for_live_upload=True,
                    source="uploaded",
                )
                self._mark_stage(
                    report,
                    "queued_ready",
                    True,
                    queue_id=upload_result.get("queue_id"),
                    status=uploaded_item.get("status"),
                    source="uploaded",
                )
            LOGGER.info(
                "uploaded_count=%s upload_status=%s queue_id=%s",
                report["uploaded_count"],
                upload_result.get("status"),
                upload_result.get("queue_id"),
            )
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
        existing_limit = 0 if self.pipeline.publisher.real_upload_enabled else max_attempts
        existing_items = self.pipeline.db.list_for_processing(limit=existing_limit)
        for item in existing_items:
            result = self._process_generation_item(item)
            attempts.append(result)
            if result.get("upload_ready"):
                return self._generation_summary(attempts, result)

        for _ in range(max_attempts - len(attempts)):
            item = self.pipeline.discover_and_queue(bypass_pending_limit=True)
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
        safety_payload = self._json_object(processed.get("safety_json"))
        upload_ready = bool(
            processed.get("status") == QueueStatus.READY
            and approved_for_live_upload
            and processed.get("video_path")
            and commercial_rights_verified
        )
        diagnostics = self._attempt_diagnostics(
            processed=processed,
            safety_payload=safety_payload,
            approved_for_live_upload=approved_for_live_upload,
            commercial_rights_verified=commercial_rights_verified,
            upload_ready=upload_ready,
        )
        return {
            "generation_attempted": True,
            "queue_id": queue_id,
            "title": processed.get("title"),
            "status": processed.get("status"),
            "quality_score": processed.get("quality_score"),
            "approved_for_live_upload": approved_for_live_upload,
            "background_filename": processed.get("background_filename"),
            "background_commercial_rights_verified": commercial_rights_verified,
            "upload_blocked_reason": processed.get("upload_blocked_reason"),
            "upload_ready": upload_ready,
            "video_rendered": bool(processed.get("video_path")),
            "validation_passed": bool(processed.get("status") == QueueStatus.READY and approved_for_live_upload),
            **diagnostics,
        }

    def _attempt_diagnostics(
        self,
        *,
        processed: dict[str, Any],
        safety_payload: dict[str, Any],
        approved_for_live_upload: bool,
        commercial_rights_verified: bool,
        upload_ready: bool,
    ) -> dict[str, Any]:
        reason = str(processed.get("upload_blocked_reason") or processed.get("error") or "").strip()
        status = str(processed.get("status") or "")
        checks = safety_payload.get("checks") if isinstance(safety_payload.get("checks"), dict) else {}
        quality_score = self._float_or_none(
            processed.get("quality_score", safety_payload.get("quality_score"))
        )
        safety_score = self._float_or_none(safety_payload.get("safety_score"))
        quality_threshold = float(
            self.config.get("publishing", {}).get("min_upload_quality_score", 75)
        )
        safety_threshold = float(
            self.config.get("publishing", {}).get("min_upload_safety_score", 90)
        )
        video_rendered = bool(processed.get("video_path"))
        score_reasons = safety_payload.get("score_reasons")
        if not isinstance(score_reasons, list):
            score_reasons = []
        language_evaluated = bool(checks.get("required_language")) or any(
            "language" in str(item).lower() for item in score_reasons
        )
        pt_br_passed = None
        if checks:
            pt_br_passed = all(
                str(checks.get(key) or "") == "pt-BR"
                for key in [
                    "script_language",
                    "narration_language",
                    "subtitle_language",
                    "title_language",
                    "description_language",
                    "hashtag_language",
                ]
            ) and not checks.get("english_residue")
        if "English text remains" in reason or "must be pt-BR" in reason:
            pt_br_passed = False
        elif language_evaluated and pt_br_passed is None:
            pt_br_passed = True

        cta_passed = None
        if checks:
            manipulative_hits = checks.get("manipulative_engagement_hits") or []
            cta_passed = bool(
                checks.get("engagement_prompt_count") == 1
                and checks.get("engagement_prompt_near_end") is True
                and not manipulative_hits
            )
        if "engagement" in reason.lower():
            cta_passed = False

        duplicate_passed = None
        if "duplicate" in reason.lower() or "similar to previous" in reason.lower():
            duplicate_passed = False
        elif safety_payload:
            duplicate_passed = True

        quality_passed = None if quality_score is None else quality_score >= quality_threshold
        safety_passed = None if safety_score is None else safety_score >= safety_threshold
        validation_passed = bool(status == QueueStatus.READY and approved_for_live_upload)

        gates = {
            "story_selected": {"passed": True, "detail": "queue item was selected"},
            "video_rendered": {
                "passed": video_rendered,
                "detail": f"video_path_present={video_rendered}",
            },
            "validation_passed": {
                "passed": validation_passed,
                "detail": f"status={status}; approved_for_live_upload={approved_for_live_upload}",
            },
            "queued_ready": {
                "passed": upload_ready,
                "detail": f"status={status}; upload_ready={upload_ready}",
            },
            "quality_score": {
                "passed": quality_passed,
                "score": quality_score,
                "minimum": quality_threshold,
            },
            "safety_score": {
                "passed": safety_passed,
                "score": safety_score,
                "minimum": safety_threshold,
            },
            "pt-BR": {
                "passed": pt_br_passed,
                "detail": self._language_detail(checks),
            },
            "CTA": {
                "passed": cta_passed,
                "detail": self._cta_detail(checks),
            },
            "duplicidade": {
                "passed": duplicate_passed,
                "detail": "local duplicate script check",
            },
            "direitos_comerciais": {
                "passed": commercial_rights_verified,
                "detail": f"background_commercial_rights_verified={commercial_rights_verified}",
            },
        }
        failure_stage = self._failure_stage(
            status=status,
            reason=reason,
            gates=gates,
            video_rendered=video_rendered,
        )
        return {
            "failure_stage": failure_stage,
            "failure_reason": reason or ("ready for upload" if upload_ready else "not ready for upload"),
            "quality_score": quality_score,
            "safety_score": safety_score,
            "attempt_gates": gates,
            "content_safety_reasons": list(safety_payload.get("reasons") or []),
        }

    def _failure_stage(
        self,
        *,
        status: str,
        reason: str,
        gates: dict[str, Any],
        video_rendered: bool,
    ) -> str:
        lower_reason = reason.lower()
        if status in {
            QueueStatus.NEEDS_RESEARCH,
            QueueStatus.NEEDS_FRESH_SOURCE,
            QueueStatus.NEEDS_TRUSTED_SOURCE,
        }:
            return "research"
        if "english text remains" in lower_reason or "must be pt-br" in lower_reason:
            return "pt-BR"
        if "engagement" in lower_reason:
            return "CTA"
        if "duplicate" in lower_reason or "similar to previous" in lower_reason:
            return "duplicidade"
        if gates["quality_score"]["passed"] is False:
            return "quality_score"
        if gates["safety_score"]["passed"] is False:
            return "safety_score"
        if not video_rendered:
            return "video_rendered"
        if gates["direitos_comerciais"]["passed"] is False:
            return "direitos_comerciais"
        if gates["validation_passed"]["passed"] is False:
            return "validation_passed"
        if gates["queued_ready"]["passed"] is False:
            return "queued_ready"
        return "none"

    def _json_object(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _float_or_none(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _language_detail(self, checks: dict[str, Any]) -> str:
        if not checks:
            return "language gate not evaluated"
        residue = checks.get("english_residue") or []
        return (
            f"script={checks.get('script_language')} narration={checks.get('narration_language')} "
            f"subtitle={checks.get('subtitle_language')} title={checks.get('title_language')} "
            f"english_residue_count={len(residue)}"
        )

    def _cta_detail(self, checks: dict[str, Any]) -> str:
        if not checks:
            return "CTA gate not evaluated"
        return (
            f"count={checks.get('engagement_prompt_count')} "
            f"near_end={checks.get('engagement_prompt_near_end')} "
            f"engagement_score={checks.get('engagement_score')}"
        )

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

    def _empty_workflow_stages(self) -> dict[str, Any]:
        return {
            "story_selected": {"passed": False, "detail": ""},
            "video_rendered": {"passed": False, "detail": ""},
            "validation_passed": {"passed": False, "detail": ""},
            "queued_ready": {"passed": False, "detail": ""},
            "upload_attempted": {"passed": False, "detail": ""},
        }

    def _apply_generation_stages(self, report: dict[str, Any], generation: dict[str, Any] | None) -> None:
        generation = generation or {}
        attempts = generation.get("attempts") if isinstance(generation.get("attempts"), list) else []
        selected = generation
        if attempts:
            selected = next((attempt for attempt in attempts if attempt.get("upload_ready")), attempts[-1])
        if selected.get("queue_id"):
            self._mark_stage(
                report,
                "story_selected",
                True,
                queue_id=selected.get("queue_id"),
                title=selected.get("title"),
            )
        self._mark_stage(
            report,
            "video_rendered",
            bool(selected.get("video_rendered")),
            queue_id=selected.get("queue_id"),
            video_path_present=bool(selected.get("video_rendered")),
        )
        self._mark_stage(
            report,
            "validation_passed",
            bool(selected.get("validation_passed")),
            queue_id=selected.get("queue_id"),
            quality_score=selected.get("quality_score"),
            approved_for_live_upload=selected.get("approved_for_live_upload"),
            blocked_reason=selected.get("upload_blocked_reason"),
        )
        self._mark_stage(
            report,
            "queued_ready",
            bool(selected.get("upload_ready")),
            queue_id=selected.get("queue_id"),
            status=selected.get("status"),
            commercial_rights_verified=selected.get("background_commercial_rights_verified"),
        )

    def _mark_stage(self, report: dict[str, Any], stage: str, passed: bool, **details: Any) -> None:
        detail = " ".join(f"{key}={value}" for key, value in details.items() if value is not None)
        report.setdefault("workflow_stages", self._empty_workflow_stages())[stage] = {
            "passed": bool(passed),
            "detail": detail,
            **details,
        }
        LOGGER.info("%s=%s %s", stage, str(bool(passed)).lower(), detail)

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
