from __future__ import annotations

import html
import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.models import ContentScript, QueueStatus, ResearchBrief, TrendTopic
from app.services.background_library import BackgroundLibraryUnavailableError
from app.services.config import resolve_storage_path
from app.services.alerts import TelegramAlertClient
from app.services.pipeline import ShortsMasterPipeline
from app.utils.text import clean_text


LOGGER = logging.getLogger(__name__)


class YouTubePublishScheduler:
    RETRYABLE_UPLOAD_PAUSE_MARKERS = (
        "consecutive upload failures",
        "consecutive upload failure",
        "upload failures",
    )

    def __init__(self, pipeline: ShortsMasterPipeline, config: dict[str, Any]):
        self.pipeline = pipeline
        self.config = config
        self.db = pipeline.db
        self.publishing = config.get("publishing", {})
        self.scheduler_config = config.get("scheduler", {})
        self.alerts = TelegramAlertClient(config)
        self.timezone = ZoneInfo(config.get("app", {}).get("timezone", "UTC"))

    def publish_next_ready(self) -> dict[str, Any]:
        auto_resume_result: dict[str, Any] | None = None
        if self.is_paused():
            auto_resume_result = self.auto_resume_if_retryable_upload_pause()
            if not auto_resume_result.get("resumed"):
                result = {
                    "status": "paused",
                    "reason": self.pause_reason(),
                    "uploaded": False,
                    "auto_resume": auto_resume_result,
                }
                LOGGER.warning("YouTube upload scheduler is paused: %s", result["reason"])
                self.write_report(self.build_report(last_result=result))
                return result

        max_attempts = int(self.scheduler_config.get("max_upload_attempts_per_video", 3))
        item = self.db.next_upload_candidate(max_attempts=max_attempts)
        if item is None:
            reason = "No approved or ready videos are waiting in queue"
            LOGGER.info("YouTube upload scheduler skipped: %s", reason)
            self.db.record_scheduler_event("queue", "empty", reason=reason)
            self.alerts.send("queue empty", reason)
            result = {"status": "empty", "reason": reason, "uploaded": False}
            self._attach_auto_resume(result, auto_resume_result)
            self.write_report(self.build_report(last_result=result))
            return result

        queue_id = int(item["id"])
        LOGGER.info("YouTube upload scheduler selected oldest candidate #%s: %s", queue_id, item["title"])
        resolved_video_path = self.pipeline.resolve_existing_video_path(item)
        if (
            item["status"] == QueueStatus.APPROVED
            or not item.get("video_path")
            or not item.get("script_json")
            or resolved_video_path is None
            or not resolved_video_path.exists()
        ):
            try:
                item = self.pipeline.process_item(item, publish_after_generate=False)
            except BackgroundLibraryUnavailableError as exc:
                reason = str(exc)
                self.db.mark_failed(queue_id, reason)
                result = {
                    "status": "paused",
                    "queue_id": queue_id,
                    "reason": reason,
                    "uploaded": False,
                }
                self._attach_auto_resume(result, auto_resume_result)
                self.write_report(self.build_report(last_result=result))
                return result
            if item.get("status") != QueueStatus.READY:
                reason = f"queue item #{queue_id} is not upload-ready after generation; status={item.get('status')}"
                return self._skip(
                    queue_id,
                    "validation",
                    "validation_failed",
                    reason,
                    item,
                    auto_resume=auto_resume_result,
                )

        topic = self.pipeline._topic_from_queue_item(item)
        script = ContentScript.from_dict(json.loads(item["script_json"]))
        video_path = self.pipeline.resolve_existing_video_path(item) or Path(item["video_path"])

        gate = self.pipeline.safety.evaluate_upload(
            self.pipeline.publisher.real_upload_enabled,
            queue_id=queue_id,
            topic=topic,
            script=script,
            video_path=video_path,
            queue_item=item,
        )
        if not self.pipeline.publisher.real_upload_enabled:
            reason = "real upload disabled; PAPER_MODE, ENABLE_REAL_UPLOAD, and LIVE_UPLOAD_ENABLED must explicitly allow live upload"
            return self._skip(queue_id, "upload_gate", "skipped", reason, item, gate, auto_resume_result)
        if not gate["allowed"]:
            reason = "; ".join(gate.get("reasons") or gate.get("checklist", {}).get("real_upload_blockers", []))
            event_status = "validation_failed" if self._is_validation_gate_failure(reason) else "skipped"
            result = self._skip(queue_id, "upload_gate", event_status, reason, item, gate, auto_resume_result)
            if "youtube_quota_available" in reason:
                self.alerts.send("quota limit reached", reason)
            if event_status == "validation_failed":
                self._pause_if_validation_failure_rate_too_high()
            return result

        attempts = self.db.increment_upload_attempt(queue_id)
        try:
            youtube_id = self.pipeline.publisher.publish(video_path, script, item)
        except Exception as exc:
            reason = f"upload failed for queue item #{queue_id}: {exc}"
            terminal = attempts >= max_attempts
            self.db.mark_upload_failure(queue_id, reason, terminal=terminal)
            self.db.record_scheduler_event(
                "upload",
                "failure",
                reason=reason,
                queue_id=queue_id,
                payload={"attempt": attempts, "terminal": terminal},
            )
            LOGGER.exception(reason)
            self.alerts.send("upload failure", reason)
            self._pause_if_consecutive_upload_failures()
            result = {"status": "failure", "queue_id": queue_id, "reason": reason, "uploaded": False, "attempt": attempts}
            self._attach_auto_resume(result, auto_resume_result)
            self.write_report(self.build_report(last_result=result))
            return result

        self.db.record_youtube_quota_usage(
            queue_id,
            "videos.insert",
            int(self.publishing.get("youtube_upload_quota_units", 1600)),
        )
        self.db.mark_published(queue_id, youtube_id, paper_mode=False)
        self.db.record_metrics(
            queue_id=queue_id,
            youtube_video_id=youtube_id,
            niche=topic.niche,
            views=0,
            likes=0,
            comments=0,
        )
        result = {"status": "success", "queue_id": queue_id, "youtube_video_id": youtube_id, "uploaded": True}
        self._attach_auto_resume(result, auto_resume_result)
        self.db.record_scheduler_event("upload", "success", queue_id=queue_id, payload=result)
        self.alerts.send("upload success", f"Uploaded queue #{queue_id}: {script.title}\nVideo ID: {youtube_id}")
        self.write_report(self.build_report(last_result=result))
        return result

    def build_report(self, next_upload_time: str | None = None, last_result: dict[str, Any] | None = None) -> dict[str, Any]:
        event_counts = self.db.scheduler_event_counts_today()
        last_uploaded = self.db.last_uploaded_video()
        last_failure = self.db.last_scheduler_failure()
        report = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "paper_mode": bool(self.config.get("app", {}).get("paper_mode", True)),
            "enable_real_upload": bool(self.publishing.get("enable_real_upload", False)),
            "live_upload_enabled": bool(self.publishing.get("live_upload_enabled", False)),
            "real_upload_enabled_effective": bool(self.pipeline.publisher.real_upload_enabled),
            "privacy_status": str(self.publishing.get("privacy_status", "private")),
            "next_upload_time": next_upload_time or self.next_upload_time_iso(),
            "upload_slots": self.upload_slots(),
            "uploads_today": self.db.count_real_uploads_today(),
            "daily_upload_limit": self.daily_upload_limit(),
            "videos_waiting_in_queue": self.db.count_waiting_for_upload(),
            "success_count_today": int(event_counts.get("success", 0)),
            "failure_count_today": int(event_counts.get("failure", 0)),
            "skipped_count_today": int(event_counts.get("skipped", 0)) + int(event_counts.get("validation_failed", 0)),
            "consecutive_upload_failures": self.db.consecutive_upload_failures(),
            "validation_failure_rate": self.db.validation_failure_rate(
                int(self.scheduler_config.get("validation_failure_window", 10))
            ),
            "scheduler_paused": self.is_paused(),
            "scheduler_pause_reason": self.pause_reason(),
            "last_uploaded_video": self._queue_summary(last_uploaded),
            "last_failure_reason": last_failure.get("reason") if last_failure else "",
            "last_result": last_result or {},
        }
        return report

    def write_report(self, report: dict[str, Any]) -> dict[str, str]:
        reports_dir = resolve_storage_path(self.config, self.config.get("storage", {}).get("reports_dir", "reports"))
        reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = reports_dir / "youtube_scheduler_report.json"
        html_path = reports_dir / "youtube_scheduler_report.html"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        html_path.write_text(self._html_report(report), encoding="utf-8")
        return {"json": str(json_path), "html": str(html_path)}

    def upload_slots(self) -> list[str]:
        explicit = self.scheduler_config.get("upload_times")
        if isinstance(explicit, list) and explicit:
            return [str(item) for item in explicit[: self.daily_upload_limit()]]
        count = max(1, self.daily_upload_limit())
        start_hour = int(self.scheduler_config.get("upload_window_start_hour", 8))
        end_hour = int(self.scheduler_config.get("upload_window_end_hour", 22))
        start_minutes = start_hour * 60
        end_minutes = end_hour * 60
        if end_minutes <= start_minutes:
            end_minutes = start_minutes + 24 * 60
        if count == 1:
            minutes = [start_minutes]
        else:
            step = (end_minutes - start_minutes) / (count - 1)
            minutes = [round(start_minutes + step * index) for index in range(count)]
        return [f"{(minute // 60) % 24:02d}:{minute % 60:02d}" for minute in minutes]

    def next_upload_time_iso(self, now: datetime | None = None) -> str:
        current = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        for day_offset in range(2):
            date = current.date() + timedelta(days=day_offset)
            for slot in self.upload_slots():
                hour, minute = [int(part) for part in slot.split(":", 1)]
                candidate = datetime.combine(date, time(hour=hour, minute=minute), tzinfo=self.timezone)
                if candidate > current:
                    return candidate.isoformat()
        return (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    def daily_upload_limit(self) -> int:
        requested = int(self.publishing.get("daily_upload_limit", 5))
        hard_max = int(self.publishing.get("max_daily_upload_limit", 5))
        return max(0, min(requested, hard_max))

    def is_paused(self) -> bool:
        return bool(self.db.get_scheduler_state("youtube_upload_scheduler").get("paused", False))

    def pause_reason(self) -> str:
        return str(self.db.get_scheduler_state("youtube_upload_scheduler").get("reason", "") or "")

    def pause(self, reason: str) -> None:
        self.db.set_scheduler_state(
            "youtube_upload_scheduler",
            {"paused": True, "reason": reason, "paused_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()},
        )
        self.db.record_scheduler_event("scheduler", "paused", reason=reason)
        self.alerts.send("scheduler paused/stopped", reason)
        LOGGER.error("YouTube upload scheduler paused: %s", reason)

    def resume(self, reason: str = "manual resume", clear_upload_attempts: bool = True) -> dict[str, Any]:
        retry_reset = (
            self.reset_upload_retry_state_for_ready_candidates(reason)
            if clear_upload_attempts
            else {"cleared_count": 0, "queue_ids": []}
        )
        self.db.set_scheduler_state(
            "youtube_upload_scheduler",
            {
                "paused": False,
                "reason": "",
                "resumed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "last_resume_reason": reason,
            },
        )
        self.db.record_scheduler_event(
            "scheduler",
            "resumed",
            reason=reason,
            payload={"upload_retry_reset": retry_reset},
        )
        LOGGER.info(
            "YouTube upload scheduler resumed: reason=%s cleared_upload_retry_items=%s",
            reason,
            retry_reset["cleared_count"],
        )
        return {
            "status": "resumed",
            "paused": False,
            "reason": reason,
            "upload_retry_reset": retry_reset,
        }

    def auto_resume_if_retryable_upload_pause(self) -> dict[str, Any]:
        if not self.is_paused():
            return {
                "attempted": False,
                "resumed": False,
                "reason": "scheduler is not paused",
            }

        previous_reason = self.pause_reason()
        if not self._is_retryable_upload_failure_pause(previous_reason):
            return {
                "attempted": False,
                "resumed": False,
                "reason": "pause reason is not an upload-failure retry pause",
                "previous_pause_reason": previous_reason,
            }
        if not self.pipeline.publisher.real_upload_enabled:
            return {
                "attempted": True,
                "resumed": False,
                "reason": "real upload is not enabled, so retrying would only hit the upload guard",
                "previous_pause_reason": previous_reason,
            }

        retry_reset = self.reset_upload_retry_state_for_ready_candidates(
            "auto resume after credential/upload retry state changed"
        )
        max_attempts = int(self.scheduler_config.get("max_upload_attempts_per_video", 3))
        candidate = self.db.next_upload_candidate(max_attempts=max_attempts)
        if candidate is None:
            return {
                "attempted": True,
                "resumed": False,
                "reason": "No approved READY/APPROVED upload candidate is available after retry reset",
                "previous_pause_reason": previous_reason,
                "upload_retry_reset": retry_reset,
            }

        resume_result = self.resume(
            reason="auto resume after consecutive upload failures; retrying READY queue item",
            clear_upload_attempts=False,
        )
        LOGGER.info(
            "scheduler_auto_resume=true queue_id=%s previous_pause_reason=%s",
            candidate.get("id"),
            previous_reason,
        )
        return {
            "attempted": True,
            "resumed": True,
            "reason": "auto-resumed retryable upload failure pause",
            "previous_pause_reason": previous_reason,
            "queue_id": candidate.get("id"),
            "upload_retry_reset": retry_reset,
            "resume_result": resume_result,
        }

    def reset_upload_retry_state_for_ready_candidates(self, reason: str) -> dict[str, Any]:
        cleared_ids: list[int] = []
        for status in (QueueStatus.READY, QueueStatus.APPROVED):
            for item in self.db.list_queue(status=status, limit=200):
                if item.get("youtube_video_id"):
                    continue
                if int(item.get("approved_for_live_upload") or 0) != 1:
                    continue
                has_retry_state = bool(
                    int(item.get("upload_attempt_count") or 0) > 0
                    or item.get("last_upload_error")
                    or item.get("upload_blocked_reason")
                    or item.get("error")
                )
                if not has_retry_state:
                    continue
                queue_id = int(item["id"])
                self.db.update_queue_item(
                    queue_id,
                    upload_attempt_count=0,
                    last_upload_error=None,
                    upload_blocked_reason=None,
                    error=None,
                )
                cleared_ids.append(queue_id)

        if cleared_ids:
            self.db.record_scheduler_event(
                "scheduler",
                "retry_state_reset",
                reason=reason,
                payload={"queue_ids": cleared_ids},
            )
        return {"cleared_count": len(cleared_ids), "queue_ids": cleared_ids}

    def _skip(
        self,
        queue_id: int,
        event_type: str,
        status: str,
        reason: str,
        item: dict[str, Any],
        payload: dict[str, Any] | None = None,
        auto_resume: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reason = clean_text(reason) or "scheduler skipped queue item"
        LOGGER.warning("YouTube scheduler skipped queue item #%s: %s", queue_id, reason)
        self.db.update_queue_item(queue_id, upload_blocked_reason=reason, error=reason)
        self.db.record_scheduler_event(event_type, status, reason=reason, queue_id=queue_id, payload=payload or {})
        result = {
            "status": status,
            "queue_id": queue_id,
            "topic": item.get("title"),
            "reason": reason,
            "uploaded": False,
        }
        self._attach_auto_resume(result, auto_resume)
        self.write_report(self.build_report(last_result=result))
        return result

    def _is_validation_gate_failure(self, reason: str) -> bool:
        validation_markers = [
            "quality_score_minimum",
            "safety_score_minimum",
            "trust_score_minimum",
            "freshness_score_current_topic",
            "duplicate_upload_absent",
            "duplicate_title_absent",
            "video_file_exists",
            "background_commercial_rights_verified",
        ]
        return any(marker in reason for marker in validation_markers)

    def _is_retryable_upload_failure_pause(self, reason: str) -> bool:
        lower_reason = reason.lower()
        return any(marker in lower_reason for marker in self.RETRYABLE_UPLOAD_PAUSE_MARKERS)

    def _attach_auto_resume(self, result: dict[str, Any], auto_resume: dict[str, Any] | None) -> None:
        if auto_resume:
            result["auto_resume"] = auto_resume

    def _pause_if_consecutive_upload_failures(self) -> None:
        failures = self.db.consecutive_upload_failures()
        limit = int(self.scheduler_config.get("max_consecutive_upload_failures", 3))
        if failures >= limit:
            self.pause(f"stopped after {failures} consecutive upload failures")

    def _pause_if_validation_failure_rate_too_high(self) -> None:
        window = int(self.scheduler_config.get("validation_failure_window", 10))
        min_events = int(self.scheduler_config.get("validation_failure_min_events", 5))
        max_rate = float(self.scheduler_config.get("max_validation_failure_rate", 0.5))
        stats = self.db.validation_failure_rate(window)
        if int(stats["total"]) >= min_events and float(stats["rate"]) > max_rate:
            self.pause(
                f"stopped because validation failure rate is too high: "
                f"{stats['failures']}/{stats['total']} ({stats['rate']:.2%})"
            )

    def _queue_summary(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {
            "id": item.get("id"),
            "title": item.get("title"),
            "background_category": item.get("background_category"),
            "background_filename": item.get("background_filename"),
            "youtube_video_id": item.get("youtube_video_id"),
            "published_at": item.get("published_at"),
        }

    def _html_report(self, report: dict[str, Any]) -> str:
        rows = []
        for key, value in report.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, indent=2, ensure_ascii=True)
            rows.append(
                "<tr>"
                f"<th>{html.escape(str(key))}</th>"
                f"<td><pre>{html.escape(str(value))}</pre></td>"
                "</tr>"
            )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>YouTube Scheduler Report</title>"
            "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
            "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}"
            "th{width:280px;background:#f4f4f4}pre{white-space:pre-wrap;margin:0}</style></head>"
            "<body><h1>YouTube Scheduler Report</h1>"
            f"<table>{''.join(rows)}</table></body></html>"
        )
