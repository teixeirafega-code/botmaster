from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.generators.images import ImageGenerator
from app.generators.script import ScriptGenerator
from app.generators.video import VideoAssembler
from app.generators.voice import VoiceGenerator
from app.models import ContentScript, QueueStatus, ResearchBrief, TrendTopic
from app.publisher import YouTubePublisher
from app.services.background_library import BackgroundLibraryManager
from app.services.config import resolve_path
from app.services.database import ShortsMasterDatabase
from app.services.reddit_story import RedditStoryService, is_reddit_story_topic
from app.services.research import ResearchService
from app.services.safety import SafetyGuard
from app.services.topic_selector import TopicSelector
from app.services.trend_service import TrendService
from app.tracker import MetricsTracker


LOGGER = logging.getLogger(__name__)


class ShortsMasterPipeline:
    def __init__(self, config: dict):
        self.config = config
        db_path = resolve_path(config, config["app"]["database_path"])
        self.db = ShortsMasterDatabase(db_path)
        self.trends = TrendService(config)
        self.selector = TopicSelector(self.db, config)
        self.research = ResearchService(config)
        self.story_sources = RedditStoryService(config)
        self.scripts = ScriptGenerator(config)
        self.voice = VoiceGenerator(config)
        self.images = ImageGenerator(config)
        self.background_library = BackgroundLibraryManager(config, self.db)
        self.video = VideoAssembler(config, background_library=self.background_library)
        self.publisher = YouTubePublisher(config)
        self.metrics = MetricsTracker(self.db, self.publisher)
        self.safety = SafetyGuard(self.db, config)
        self.paper_mode = bool(config.get("app", {}).get("paper_mode", True))

    def run_cycle(self) -> dict[str, Any]:
        result: dict[str, Any] = {"queued": None, "processed": 0}
        result["queued"] = self.discover_and_queue()
        if self.config.get("queue", {}).get("auto_process_approved", True):
            result["processed"] = self.process_approved(limit=3)
        return result

    def discover_and_queue(self, bypass_pending_limit: bool = False) -> dict[str, Any] | None:
        max_pending = int(self.config.get("queue", {}).get("max_pending", 25))
        if not bypass_pending_limit and self.db.pending_count() >= max_pending:
            LOGGER.info("Queue has reached max pending count %s; skipping discovery", max_pending)
            return None

        best = self._select_next_topic()
        if best is None:
            return None

        status = (
            QueueStatus.PENDING_APPROVAL
            if self.config.get("queue", {}).get("manual_approval", True)
            else QueueStatus.APPROVED
        )
        self.db.mark_seen(best, selected=True)
        item = self.db.enqueue_topic(best, status=status)
        LOGGER.info("Queued topic #%s: %s", item.get("id"), best.title)
        return item

    def _select_next_topic(self) -> TrendTopic | None:
        if self.story_sources.enabled:
            candidates = self.story_sources.fetch_candidates()
            candidates.sort(key=lambda item: item.selection_score, reverse=True)
            topics = [self.story_sources.topic_from_candidate(candidate) for candidate in candidates]
            self.db.record_observations(topics)
            for story in topics:
                if not self.db.is_seen(story.key):
                    return story
            if topics:
                LOGGER.info("All eligible Reddit story candidates were already seen")
                return None
            else:
                LOGGER.warning("Reddit story mode found no eligible story candidates")
                return None

        topics = self.trends.collect()
        return self.selector.select_best(topics)

    def approve(self, queue_id: int) -> dict[str, Any]:
        item = self.db.approve(queue_id)
        LOGGER.info("Approved queue item #%s", queue_id)
        return item

    def process_approved(self, limit: int = 3, publish_after_generate: bool | None = None) -> int:
        processed = 0
        for item in self.db.list_for_processing(limit=limit):
            try:
                self.process_item(item, publish_after_generate=publish_after_generate)
                processed += 1
            except Exception as exc:
                LOGGER.exception("Queue item #%s failed", item["id"])
                self.db.mark_failed(int(item["id"]), str(exc))
        return processed

    def process_item(self, item: dict[str, Any], publish_after_generate: bool | None = None) -> dict[str, Any]:
        queue_id = int(item["id"])
        status = item["status"]
        topic = self._topic_from_queue_item(item)
        if publish_after_generate is None:
            publish_after_generate = not self.publisher.real_upload_enabled
        research = self._research_for_item(item, topic)
        if not research.has_enough_facts:
            reason = research.missing_facts_reason()
            LOGGER.warning("Queue item #%s needs research: %s", queue_id, reason)
            self.db.mark_needs_research(queue_id, reason, research.to_dict())
            return self.db.get_queue_item(queue_id) or item
        if research.needs_fresh_source:
            reason = research.stale_source_reason()
            LOGGER.warning("Queue item #%s needs a fresh source: %s", queue_id, reason)
            self.db.mark_needs_fresh_source(queue_id, reason, research.to_dict())
            return self.db.get_queue_item(queue_id) or item
        if research.needs_trusted_source:
            reason = research.untrusted_source_reason()
            LOGGER.warning("Queue item #%s needs a trusted source: %s", queue_id, reason)
            self.db.mark_needs_trusted_source(queue_id, reason, research.to_dict())
            return self.db.get_queue_item(queue_id) or item
        if status == QueueStatus.READY and item.get("video_path") and item.get("script_json"):
            script = ContentScript.from_dict(json.loads(item["script_json"]))
            video_path = Path(item["video_path"])
        else:
            self.db.update_queue_item(queue_id, status=QueueStatus.GENERATING, error=None)
            script = self._script_for_item(item, topic, research)
            language_decision = self.safety.evaluate_language(script)
            if not language_decision["allowed"]:
                decision = self._language_block_decision(language_decision)
                reason = "; ".join(decision["reasons"])
                self.db.update_queue_item(
                    queue_id,
                    research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
                    script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
                )
                self.db.mark_safety_blocked(queue_id, reason, decision)
                LOGGER.warning("Queue item #%s blocked before render by language gate: %s", queue_id, reason)
                return self.db.get_queue_item(queue_id) or item
            voice_path = self.voice.generate(script, queue_id)
            if is_reddit_story_topic(topic) and self.video.uses_story_background:
                self.images.last_sources = []
                self.images.last_warnings = []
                image_paths = []
            else:
                image_paths = self.images.generate(script, topic, queue_id)
            video_path = self.video.assemble(script, image_paths, voice_path, queue_id)
            self.db.update_queue_item(
                queue_id,
                status=QueueStatus.READY,
                research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
                script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
                video_path=str(video_path),
                background_category=self.video.last_background_category or None,
                background_filename=self.video.last_background_filename or None,
                background_source_url=self.video.last_background_source_url or None,
                background_license_type=self.video.last_background_license_type or None,
                background_commercial_rights_verified=int(
                    self.video.last_background_commercial_rights_verified
                ),
                engagement_prompt=script.engagement_prompt or None,
                engagement_prompt_type=script.engagement_prompt_type or None,
                engagement_score=float(script.engagement_score or 0.0),
                engagement_prompt_variants_json=json.dumps(
                    script.engagement_prompt_variants,
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
            if self.video.last_background_mode == "custom_background_library":
                self.background_library.write_report()

        content_decision = self.safety.evaluate_content(queue_id, topic, script)
        self.db.update_queue_item(
            queue_id,
            quality_score=float(content_decision["quality_score"]),
            safety_json=json.dumps(content_decision, ensure_ascii=True, sort_keys=True),
            upload_blocked_reason=None if content_decision["allowed"] else "; ".join(content_decision["reasons"]),
        )
        if not content_decision["allowed"]:
            reason = "; ".join(content_decision["reasons"])
            self.db.mark_safety_blocked(queue_id, reason, content_decision)
            return self.db.get_queue_item(queue_id) or item

        self._apply_auto_live_approval(queue_id, content_decision)

        if not publish_after_generate:
            LOGGER.info("Queue item #%s is ready and waiting for scheduled YouTube upload", queue_id)
            return self.db.get_queue_item(queue_id) or item

        latest_item = self.db.get_queue_item(queue_id) or item
        upload_decision = self.safety.evaluate_upload(
            self.publisher.real_upload_enabled,
            queue_id=queue_id,
            topic=topic,
            script=script,
            video_path=video_path,
            queue_item=latest_item,
        )
        if not upload_decision["allowed"]:
            reason = "; ".join(upload_decision["reasons"])
            self.db.update_queue_item(
                queue_id,
                status=QueueStatus.READY,
                error=reason,
                upload_blocked_reason=reason,
                safety_json=json.dumps(
                    {"content": content_decision, "upload": upload_decision},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
            LOGGER.warning("Upload blocked for queue item #%s: %s", queue_id, reason)
            return self.db.get_queue_item(queue_id) or item

        youtube_id = self.publisher.publish(video_path, script, latest_item)
        if self.publisher.real_upload_enabled:
            self.db.record_youtube_quota_usage(
                queue_id,
                "videos.insert",
                int(self.config.get("publishing", {}).get("youtube_upload_quota_units", 1600)),
            )
        self.db.mark_published(
            queue_id,
            youtube_id,
            paper_mode=not self.publisher.real_upload_enabled,
        )
        updated = self.db.get_queue_item(queue_id)
        return updated or item

    def _apply_auto_live_approval(
        self,
        queue_id: int,
        content_decision: dict[str, Any],
    ) -> bool:
        if not self.config.get("queue", {}).get("auto_approve_live_upload", False):
            return False

        item = self.db.get_queue_item(queue_id) or {}
        quality_score = float(content_decision.get("quality_score", 0.0) or 0.0)
        safety_score = float(content_decision.get("safety_score", 0.0) or 0.0)
        min_quality = float(self.config.get("publishing", {}).get("min_upload_quality_score", 75))
        min_safety = float(self.config.get("publishing", {}).get("min_upload_safety_score", 90))
        commercial_rights_verified = bool(
            int(item.get("background_commercial_rights_verified") or 0) == 1
        )
        reasons = []
        if not self.publisher.real_upload_enabled:
            reasons.append("production upload gate is not active")
        if quality_score < min_quality:
            reasons.append(f"quality_score={quality_score:.1f} is below {min_quality:.1f}")
        if safety_score < min_safety:
            reasons.append(f"safety_score={safety_score:.1f} is below {min_safety:.1f}")
        if not commercial_rights_verified:
            reasons.append("background commercial rights are not verified")

        if reasons:
            reason = "auto live approval blocked: " + "; ".join(reasons)
            self.db.update_queue_item(
                queue_id,
                approved_for_live_upload=0,
                live_approved_at=None,
                upload_blocked_reason=reason,
            )
            LOGGER.warning("Queue item #%s %s", queue_id, reason)
            return False

        self.db.approve_for_live_upload(queue_id)
        LOGGER.info(
            "Queue item #%s auto-approved for live upload: quality=%.1f safety=%.1f rights_verified=true",
            queue_id,
            quality_score,
            safety_score,
        )
        return True

    def refresh_metrics(self) -> int:
        updated = self.metrics.refresh()
        self.background_library.write_report()
        return updated

    def _script_for_item(
        self,
        item: dict[str, Any],
        topic: TrendTopic,
        research: ResearchBrief | None = None,
    ) -> ContentScript:
        if item.get("script_json"):
            return ContentScript.from_dict(json.loads(item["script_json"]))
        return self.scripts.generate(topic, research)

    def _language_block_decision(self, language_decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "allowed": False,
            "quality_score": 0.0,
            "safety_score": 0.0,
            "score_reasons": ["generation stopped before narration/video because mandatory pt-BR validation failed"],
            "reasons": list(language_decision["reasons"]),
            "warnings": list(language_decision.get("warnings", [])),
            "checks": {
                "required_language": language_decision["required_language"],
                "script_language": language_decision["script_language"],
                "narration_language": language_decision["narration_language"],
                "subtitle_language": language_decision["subtitle_language"],
                "title_language": language_decision["title_language"],
                "description_language": language_decision["description_language"],
                "hashtag_language": language_decision["hashtag_language"],
                "english_residue": language_decision["english_residue"],
            },
        }

    def _research_for_item(self, item: dict[str, Any], topic: TrendTopic) -> ResearchBrief:
        if item.get("research_json"):
            return ResearchBrief.from_dict(json.loads(item["research_json"]))
        if is_reddit_story_topic(topic):
            research = self.story_sources.build_research_brief(topic)
        else:
            research = self.research.build_brief(topic)
        self.db.update_queue_item(
            int(item["id"]),
            research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
        )
        return research

    def _topic_from_queue_item(self, item: dict[str, Any]) -> TrendTopic:
        payload = json.loads(item["payload_json"])
        return TrendTopic(
            source=payload["source"],
            title=payload["title"],
            score=float(payload["score"]),
            niche=payload.get("niche", item["niche"]),
            url=payload.get("url", ""),
            volume=int(payload.get("volume", 0)),
            engagement=float(payload.get("engagement", 0.0)),
            hashtags=list(payload.get("hashtags", [])),
            raw=dict(payload.get("raw", {})),
            observed_at=payload.get("observed_at"),
        )
