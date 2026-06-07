from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.services.pipeline import ShortsMasterPipeline
from app.services.botmaster_status import BotmasterStatusClient
from app.services.youtube_publish_scheduler import YouTubePublishScheduler


LOGGER = logging.getLogger(__name__)


class ShortsMasterScheduler:
    def __init__(self, pipeline: ShortsMasterPipeline, config: dict):
        self.pipeline = pipeline
        self.config = config
        self.scheduler = BlockingScheduler(timezone=config.get("app", {}).get("timezone", "UTC"))
        self.youtube_publish_scheduler = YouTubePublishScheduler(pipeline, config)
        self.status = BotmasterStatusClient()

    def start(self) -> None:
        self.pipeline.background_library.resume_publishing_if_library_available()
        trend_minutes = int(self.config.get("scheduler", {}).get("trend_interval_minutes", 30))
        metrics_minutes = int(self.config.get("scheduler", {}).get("metrics_interval_minutes", 120))
        heartbeat_minutes = int(self.config.get("scheduler", {}).get("heartbeat_interval_minutes", 5))
        self.scheduler.add_job(
            self.pipeline.run_cycle,
            trigger=IntervalTrigger(minutes=trend_minutes),
            id="trend-cycle",
            name="Collect trends and process approved queue",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.pipeline.refresh_metrics,
            trigger=IntervalTrigger(minutes=metrics_minutes),
            id="metrics-refresh",
            name="Refresh YouTube metrics",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._heartbeat,
            trigger=IntervalTrigger(minutes=heartbeat_minutes),
            id="health-heartbeat",
            name="Render worker health heartbeat",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._schedule_youtube_uploads()
        LOGGER.info(
            "Starting ShortsMaster scheduler: paper_mode=%s real_upload_enabled=%s live_upload_enabled=%s",
            self.pipeline.paper_mode,
            self.pipeline.publisher.real_upload_enabled,
            self.pipeline.publisher.live_upload_enabled,
        )
        self.youtube_publish_scheduler.write_report(self.youtube_publish_scheduler.build_report())
        self._heartbeat()
        self.pipeline.run_cycle()
        self.scheduler.start()

    def _schedule_youtube_uploads(self) -> None:
        for index, slot in enumerate(self.youtube_publish_scheduler.upload_slots(), start=1):
            hour, minute = [int(part) for part in slot.split(":", 1)]
            self.scheduler.add_job(
                self._run_youtube_upload_slot,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=self.config.get("app", {}).get("timezone", "UTC")),
                id=f"youtube-upload-{index}",
                name=f"YouTube upload slot {index} ({slot})",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        LOGGER.info("Scheduled YouTube upload slots: %s", ", ".join(self.youtube_publish_scheduler.upload_slots()))

    def _run_youtube_upload_slot(self) -> None:
        result = self.youtube_publish_scheduler.publish_next_ready()
        if result.get("status") == "paused":
            self._pause_youtube_upload_jobs()
        if self.youtube_publish_scheduler.is_paused():
            self._pause_youtube_upload_jobs()

    def _pause_youtube_upload_jobs(self) -> None:
        for job in self.scheduler.get_jobs():
            if job.id.startswith("youtube-upload-"):
                self.scheduler.pause_job(job.id)
        LOGGER.error("YouTube upload jobs paused: %s", self.youtube_publish_scheduler.pause_reason())

    def _heartbeat(self) -> None:
        try:
            pending = self.pipeline.db.pending_count()
            waiting_upload = self.pipeline.db.count_waiting_for_upload()
            next_upload = self.youtube_publish_scheduler.next_upload_time_iso()
            jobs = ", ".join(sorted(job.id for job in self.scheduler.get_jobs())) or "none"
            LOGGER.info(
                "ShortsMaster heartbeat: pending_queue=%s waiting_upload=%s next_upload=%s "
                "paper_mode=%s real_upload_enabled=%s live_upload_enabled=%s scheduler_paused=%s jobs=%s",
                pending,
                waiting_upload,
                next_upload,
                self.pipeline.paper_mode,
                self.pipeline.publisher.real_upload_enabled,
                self.pipeline.publisher.live_upload_enabled,
                self.youtube_publish_scheduler.is_paused(),
                jobs,
            )
            library = self.pipeline.background_library.scan()
            credentials = self.pipeline.safety.credentials_status()
            self.status.write(
                status="RUNNING",
                metrics={
                    "pending_queue": pending,
                    "waiting_upload": waiting_upload,
                    "next_upload": next_upload,
                    "paper_mode": self.pipeline.paper_mode,
                    "real_upload_enabled": self.pipeline.publisher.real_upload_enabled,
                    "live_upload_enabled": self.pipeline.publisher.live_upload_enabled,
                    "scheduler_paused": self.youtube_publish_scheduler.is_paused(),
                    "daily_upload_limit": self.youtube_publish_scheduler.daily_upload_limit(),
                    "auto_approve_live_upload": bool(
                        self.config.get("queue", {}).get("auto_approve_live_upload", False)
                    ),
                    "commercially_cleared_backgrounds": len(library["approved_assets"]),
                    "youtube_credentials_ready": bool(
                        credentials["client_secrets_present"] and credentials["token_present"]
                    ),
                    "jobs": sorted(job.id for job in self.scheduler.get_jobs()),
                },
            )
        except Exception:
            LOGGER.exception("ShortsMaster heartbeat failed")
            self.status.write(status="ERROR", error="ShortsMaster heartbeat failed")
