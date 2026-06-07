from __future__ import annotations

import html
import json
import logging
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from app.models import ContentScript, QueueStatus, ResearchBrief, TrendTopic, utc_now_iso
from app.services.config import resolve_storage_path
from app.services.language import LanguageGuard
from app.services.narrative_style import analyze_reddit_narrative_style
from app.services.pipeline import ShortsMasterPipeline
from app.services.reddit_story import is_reddit_story_topic
from app.utils.text import clean_text, split_sentences

try:
    from moviepy import AudioFileClip, VideoFileClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioFileClip, VideoFileClip


LOGGER = logging.getLogger(__name__)


class EndToEndValidationError(RuntimeError):
    """Raised when the integration validation cannot produce a playable Short."""


class EndToEndValidator:
    def __init__(self, pipeline: ShortsMasterPipeline):
        self.pipeline = pipeline
        self.config = pipeline.config
        self.warnings: list[str] = []
        self.failures: list[str] = []
        self._apply_validation_profile()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        videos_dir = resolve_storage_path(self.config, self.config.get("storage", {}).get("videos_dir", "videos"))
        self.artifact_dir = videos_dir / "validation" / timestamp
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.story_source_report_path: Path | None = None

    def _apply_validation_profile(self) -> None:
        profile = self.config.get("validation", {})
        if not profile.get("fast_render_profile", True):
            return
        image_width = int(profile.get("image_width", 540))
        image_height = int(profile.get("image_height", 960))
        video_width = int(profile.get("video_width", 540))
        video_height = int(profile.get("video_height", 960))
        fps = int(profile.get("fps", 15))
        self.pipeline.images.image_config["width"] = image_width
        self.pipeline.images.image_config["height"] = image_height
        self.pipeline.video.video_config["width"] = video_width
        self.pipeline.video.video_config["height"] = video_height
        self.pipeline.video.video_config["fps"] = fps
        self.pipeline.video.video_config["preset"] = profile.get("preset", "ultrafast")
        self.pipeline.video.video_config["threads"] = int(profile.get("threads", 2))
        self.pipeline.video.width = video_width
        self.pipeline.video.height = video_height
        self.pipeline.video.fps = fps
        self.warnings.append(f"Validation fast render profile active: {video_width}x{video_height}@{fps}fps")
        if profile.get("force_local_images", False):
            self.pipeline.images.image_config["force_local_fallback"] = True
            self.warnings.append("Validation image generation forced local fallback for repeatable batch scoring")

    def run(self) -> dict[str, Any]:
        LOGGER.info("Starting end-to-end validation run")
        selected = self.pipeline._select_next_topic()
        if selected is None:
            raise EndToEndValidationError("No eligible unseen topic was returned by the enabled source mode")

        queued = self._queue_selected_topic(selected)
        approved = self.pipeline.approve(int(queued["id"]))
        queue_id = int(approved["id"])
        story_source_report = self._write_story_source_report(selected)

        if is_reddit_story_topic(selected):
            research = self.pipeline.story_sources.build_research_brief(selected)
        else:
            research = self.pipeline.research.build_brief(selected)
        research_path = self.artifact_dir / "research_brief.json"
        research_path.write_text(json.dumps(research.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
        self.pipeline.db.update_queue_item(
            queue_id,
            research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
        )
        if not research.has_enough_facts:
            reason = research.missing_facts_reason()
            self.pipeline.db.mark_needs_research(queue_id, reason, research.to_dict())
            return self._needs_research_report(selected, queue_id, research, reason, research_path)
        if research.needs_fresh_source:
            reason = research.stale_source_reason()
            self.pipeline.db.mark_needs_fresh_source(queue_id, reason, research.to_dict())
            return self._needs_fresh_source_report(selected, queue_id, research, reason, research_path)
        if research.needs_trusted_source:
            reason = research.untrusted_source_reason()
            self.pipeline.db.mark_needs_trusted_source(queue_id, reason, research.to_dict())
            return self._needs_trusted_source_report(selected, queue_id, research, reason, research_path)

        script = self.pipeline.scripts.generate(selected, research)
        script_path = self.artifact_dir / "generated_script.json"
        script_path.write_text(json.dumps(script.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
        language_decision = self.pipeline.safety.evaluate_language(script)
        if not language_decision["allowed"]:
            decision = self.pipeline._language_block_decision(language_decision)
            reason = "; ".join(decision["reasons"])
            self.pipeline.db.update_queue_item(
                queue_id,
                research_json=json.dumps(research.to_dict(), ensure_ascii=True, sort_keys=True),
                script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
            )
            self.pipeline.db.mark_safety_blocked(queue_id, reason, decision)
            raise EndToEndValidationError(f"Language gate blocked video generation: {reason}")

        voice_path = self.pipeline.voice.generate(script, queue_id)
        if is_reddit_story_topic(selected) and self.pipeline.video.uses_story_background:
            self.pipeline.images.last_sources = []
            self.pipeline.images.last_warnings = []
            image_paths = []
        else:
            image_paths = self.pipeline.images.generate(script, selected, queue_id)
        video_path = self.pipeline.video.assemble(script, image_paths, voice_path, queue_id)

        audio_probe = self._probe_audio(
            voice_path,
            script=script,
            generation_report=self.pipeline.voice.last_generation_report,
        )
        video_probe = self._probe_video(video_path)
        video_probe["background_mode"] = self.pipeline.video.last_background_mode
        video_probe["background_category"] = self.pipeline.video.last_background_category
        video_probe["background_tier"] = self.pipeline.video.last_background_tier
        video_probe["background_animated"] = bool(self.pipeline.video.last_background_animated)
        video_probe["subtitles_enabled"] = bool(self.pipeline.video.last_subtitles_enabled)
        video_probe["background_looped"] = bool(self.pipeline.video.last_background_looped)
        video_probe["background_trimmed"] = bool(self.pipeline.video.last_background_trimmed)
        video_probe["background_filename"] = self.pipeline.video.last_background_filename
        video_probe["background_source_url"] = self.pipeline.video.last_background_source_url
        video_probe["background_license_type"] = self.pipeline.video.last_background_license_type
        video_probe["background_commercial_rights_verified"] = bool(
            self.pipeline.video.last_background_commercial_rights_verified
        )
        if video_probe["duration_seconds"] <= 0 or video_probe["width"] <= 0 or video_probe["height"] <= 0:
            raise EndToEndValidationError("Rendered MP4 could not be probed as playable video")

        image_probe = self._probe_images(image_paths) if image_paths else self._background_probe(video_probe)
        quality_analysis = self._analyze_quality(script, selected, audio_probe, video_probe, image_probe, research)
        content_decision = self.pipeline.safety.evaluate_content(queue_id, selected, script)
        quality_blockers = list(quality_analysis.get("blocking_failures", []))
        self.pipeline.db.update_queue_item(
            queue_id,
            status=QueueStatus.READY,
            script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
            video_path=str(video_path),
            background_category=self.pipeline.video.last_background_category or None,
            background_filename=self.pipeline.video.last_background_filename or None,
            background_source_url=self.pipeline.video.last_background_source_url or None,
            background_license_type=self.pipeline.video.last_background_license_type or None,
            background_commercial_rights_verified=int(
                self.pipeline.video.last_background_commercial_rights_verified
            ),
            quality_score=float(quality_analysis["final_quality_score"]),
            safety_json=json.dumps(
                {"safety": content_decision, "quality": quality_analysis},
                ensure_ascii=True,
                sort_keys=True,
            ),
            upload_blocked_reason=None
            if content_decision["allowed"] and not quality_blockers
            else "; ".join([*content_decision["reasons"], *quality_blockers]),
        )
        if not content_decision["allowed"] or quality_blockers:
            reason = "; ".join([*content_decision["reasons"], *quality_blockers])
            self.pipeline.db.mark_safety_blocked(
                queue_id,
                reason,
                {"safety": content_decision, "quality": quality_analysis},
            )
            raise EndToEndValidationError(f"Safety blocked generated content: {reason}")

        latest_item = self.pipeline.db.get_queue_item(queue_id) or approved
        upload_decision = self.pipeline.safety.evaluate_upload(
            self.pipeline.publisher.real_upload_enabled,
            queue_id=queue_id,
            topic=selected,
            script=script,
            video_path=video_path,
            queue_item=latest_item,
        )
        if not upload_decision["allowed"]:
            reason = "; ".join(upload_decision["reasons"])
            self.pipeline.db.update_queue_item(queue_id, upload_blocked_reason=reason, error=reason)
            raise EndToEndValidationError(f"Upload gate blocked validation publish simulation: {reason}")

        upload_id = self.pipeline.publisher.publish(video_path, script, latest_item)
        if self.pipeline.publisher.real_upload_enabled:
            self.pipeline.db.record_youtube_quota_usage(
                queue_id,
                "videos.insert",
                int(self.config.get("publishing", {}).get("youtube_upload_quota_units", 1600)),
            )
        self.pipeline.db.mark_published(
            queue_id,
            upload_id,
            paper_mode=not self.pipeline.publisher.real_upload_enabled,
        )
        self.pipeline.db.record_metrics(
            queue_id=queue_id,
            youtube_video_id=upload_id,
            niche=selected.niche,
            views=0,
            likes=0,
            comments=0,
        )
        metadata = self._metadata(
            selected=selected,
            script=script,
            queue_id=queue_id,
            upload_id=upload_id,
            voice_path=voice_path,
            image_paths=image_paths,
            video_path=video_path,
            content_decision=content_decision,
            upload_decision=upload_decision,
            audio_probe=audio_probe,
            video_probe=video_probe,
            image_probe=image_probe,
            quality_analysis=quality_analysis,
            research=research,
            story_source_report=story_source_report,
        )

        artifact_paths = self._copy_artifacts(
            voice_path=voice_path,
            image_paths=image_paths,
            video_path=video_path,
            script_path=script_path,
            research_path=research_path,
            story_source_path=self.story_source_report_path,
            metadata=metadata,
        )
        metadata["artifacts"] = artifact_paths
        metadata["artifacts"].update(self._write_category_performance_report())
        metadata["artifacts"].update(self._write_background_library_report())

        metadata_path = self.artifact_dir / "metadata.json"
        report_path = self.artifact_dir / "validation_report.json"
        html_path = self.artifact_dir / "validation_report.html"
        metadata["artifacts"]["metadata_json"] = str(metadata_path)
        metadata["artifacts"]["validation_report_json"] = str(report_path)
        metadata["artifacts"]["validation_report_html"] = str(html_path)
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        report_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        html_path.write_text(self._html_report(metadata), encoding="utf-8")

        LOGGER.info("End-to-end validation completed successfully for queue #%s", queue_id)
        return metadata

    def _queue_selected_topic(self, topic: TrendTopic) -> dict[str, Any]:
        self.pipeline.db.mark_seen(topic, selected=True)
        status = (
            QueueStatus.PENDING_APPROVAL
            if self.config.get("queue", {}).get("manual_approval", True)
            else QueueStatus.APPROVED
        )
        return self.pipeline.db.enqueue_topic(topic, status=status)

    def _probe_audio(
        self,
        audio_path: Path,
        script: ContentScript | None = None,
        generation_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clip = AudioFileClip(str(audio_path))
        try:
            probe = {
                "path": str(audio_path),
                "duration_seconds": round(float(clip.duration or 0), 2),
                "file_size_bytes": audio_path.stat().st_size,
            }
        finally:
            clip.close()
        if script is not None:
            from app.services.voice_quality import analyze_voice_quality

            probe.update(analyze_voice_quality(script, audio_path, generation_report))
        return probe

    def _probe_video(self, video_path: Path) -> dict[str, Any]:
        clip = VideoFileClip(str(video_path))
        try:
            return {
                "path": str(video_path),
                "duration_seconds": round(float(clip.duration or 0), 2),
                "width": int(clip.w),
                "height": int(clip.h),
                "fps": round(float(clip.fps or 0), 2),
                "file_size_bytes": video_path.stat().st_size,
            }
        finally:
            clip.close()

    def _probe_images(self, image_paths: list[Path]) -> list[dict[str, Any]]:
        probes: list[dict[str, Any]] = []
        for index, path in enumerate(image_paths):
            with Image.open(path) as image:
                width, height = image.size
            probes.append(
                {
                    "path": str(path),
                    "width": width,
                    "height": height,
                    "file_size_bytes": path.stat().st_size,
                    "source": self.pipeline.images.last_sources[index]
                    if index < len(self.pipeline.images.last_sources)
                    else "unknown",
                }
            )
        return probes

    def _background_probe(self, video_probe: dict[str, Any]) -> list[dict[str, Any]]:
        if video_probe.get("background_mode") == "custom_background_library":
            return [
                {
                    "path": video_probe.get("background_filename"),
                    "width": int(video_probe.get("width", 0)),
                    "height": int(video_probe.get("height", 0)),
                    "file_size_bytes": 0,
                    "source": "custom_background_library",
                    "royalty_free": True,
                    "background_category": video_probe.get("background_category"),
                    "background_tier": video_probe.get("background_tier"),
                    "background_filename": video_probe.get("background_filename"),
                    "background_source_url": video_probe.get("background_source_url"),
                    "background_license_type": video_probe.get("background_license_type"),
                    "animated": bool(video_probe.get("background_animated", False)),
                    "subtitles_enabled": bool(video_probe.get("subtitles_enabled", False)),
                    "looped": bool(video_probe.get("background_looped", False)),
                    "trimmed": bool(video_probe.get("background_trimmed", False)),
                }
            ]
        if video_probe.get("background_mode") == "retention_categories":
            return [
                {
                    "path": None,
                    "width": int(video_probe.get("width", 0)),
                    "height": int(video_probe.get("height", 0)),
                    "file_size_bytes": 0,
                    "source": "procedural_retention_background",
                    "royalty_free": True,
                    "background_category": video_probe.get("background_category"),
                    "background_tier": video_probe.get("background_tier"),
                    "animated": bool(video_probe.get("background_animated", False)),
                    "subtitles_enabled": bool(video_probe.get("subtitles_enabled", False)),
                    "looped": bool(video_probe.get("background_looped", False)),
                }
            ]
        return []

    def _write_story_source_report(self, topic: TrendTopic) -> dict[str, Any] | None:
        if not is_reddit_story_topic(topic):
            return None
        report = self.pipeline.story_sources.story_source_report(topic)
        self.story_source_report_path = self.artifact_dir / "story_source_report.json"
        self.story_source_report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        return report

    def _analyze_quality(
        self,
        script: ContentScript,
        topic: TrendTopic,
        audio_probe: dict[str, Any],
        video_probe: dict[str, Any],
        image_probe: list[dict[str, Any]],
        research: ResearchBrief | None = None,
    ) -> dict[str, Any]:
        narration = clean_text(script.narration)
        words = [word.lower() for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+", narration)]
        repeated_phrases = self._repeated_phrases(words)
        sentences = split_sentences(narration)
        generic_analysis = self._generic_language_analysis(script)
        language_analysis = LanguageGuard(getattr(self, "config", {}) or {}).evaluate_script(script)
        narrative_style = (
            analyze_reddit_narrative_style(script)
            if is_reddit_story_topic(topic)
            else {
                "narrative_naturalness_score": 100.0,
                "disclaimer_leakage_score": 0.0,
                "findings": [],
                "disclaimer_hits": [],
                "report_phrase_hits": [],
            }
        )
        specificity = self._specificity_analysis(script, topic, research)
        hook = self._hook_analysis(script, topic)
        curiosity = self._curiosity_analysis(script)
        storytelling = self._storytelling_analysis(script)
        coherence_score = self._coherence_score(sentences)
        title = self._title_analysis(script.title, topic)
        retention = self._retention_analysis(script, repeated_phrases, hook)
        image = self._image_relevance_analysis(script, topic, image_probe)
        visual_interest = self._visual_interest_analysis(script, topic, video_probe, image_probe)
        factuality = self._factuality_risk_analysis(script, specificity)
        freshness = {
            "score": round(float(research.freshness_score), 1) if research else 100.0,
            "required": bool(research.requires_fresh_source) if research else False,
            "threshold": round(float(research.freshness_threshold), 1) if research else 70.0,
            "window_hours": int(research.freshness_window_hours) if research else 72,
            "findings": list(research.freshness_notes) if research else [],
        }
        trust = {
            "score": round(float(research.trust_score), 1) if research else 100.0,
            "required": bool(research.requires_trusted_source) if research else False,
            "threshold": round(float(research.trust_threshold), 1) if research else 70.0,
            "findings": list(research.trust_notes) if research else [],
        }
        av_delta = abs(float(audio_probe["duration_seconds"]) - float(video_probe["duration_seconds"]))
        av_sync = max(0.0, 100.0 - (av_delta * 20.0))
        weighted = (
            specificity["score"] * 0.11
            + hook["score"] * 0.13
            + curiosity["score"] * 0.11
            + storytelling["score"] * 0.13
            + retention["score"] * 0.14
            + visual_interest["score"] * 0.13
            + (
                narrative_style["narrative_naturalness_score"]
                if is_reddit_story_topic(topic)
                else factuality["score"]
            )
            * 0.10
            + image["score"] * 0.06
            + title["score"] * 0.06
            + coherence_score * 0.03
            + av_sync * 0.02
        )
        final_quality = round(
            max(0.0, min(98.0, weighted - generic_analysis["penalty"])),
            1,
        )
        config = getattr(self, "config", {}) or {}
        min_hook = float(config.get("safety", {}).get("min_hook_score", 70))
        min_retention = float(config.get("safety", {}).get("min_retention_score", 72))
        min_visual = float(config.get("safety", {}).get("min_visual_interest_score", 70))
        min_naturalness = float(config.get("safety", {}).get("min_narrative_naturalness_score", 75))
        blocking_failures: list[str] = []
        if hook["score"] < min_hook:
            blocking_failures.append(f"hook_score {hook['score']:.1f} below minimum {min_hook:.1f}")
            final_quality = min(final_quality, 69.0)
        if retention["score"] < min_retention:
            blocking_failures.append(f"retention_score {retention['score']:.1f} below minimum {min_retention:.1f}")
            final_quality = min(final_quality, 69.0)
        if visual_interest["score"] < min_visual:
            blocking_failures.append(f"visual_interest_score {visual_interest['score']:.1f} below minimum {min_visual:.1f}")
            final_quality = min(final_quality, 69.0)
        if not language_analysis["allowed"]:
            blocking_failures.extend(language_analysis["reasons"])
            final_quality = min(final_quality, 69.0)
        if is_reddit_story_topic(topic) and narrative_style["disclaimer_leakage_score"] > 0:
            blocking_failures.append(
                "public script contains source disclaimer language: "
                + ", ".join(narrative_style["disclaimer_hits"][:5])
            )
            final_quality = min(final_quality, 69.0)
        if (
            is_reddit_story_topic(topic)
            and narrative_style["narrative_naturalness_score"] < min_naturalness
        ):
            blocking_failures.append(
                f"narrative_naturalness_score "
                f"{narrative_style['narrative_naturalness_score']:.1f} "
                f"below minimum {min_naturalness:.1f}"
            )
            final_quality = min(final_quality, 69.0)
        legacy_overall = round(
            (
                coherence_score * 0.25
                + title["score"] * 0.2
                + retention["score"] * 0.25
                + image["score"] * 0.2
                + av_sync * 0.1
            ),
            1,
        )
        issues: list[str] = []
        if repeated_phrases:
            issues.append("Repeated phrases detected: " + ", ".join(item["phrase"] for item in repeated_phrases[:3]))
        issues.extend(generic_analysis["findings"])
        issues.extend(language_analysis["reasons"])
        issues.extend(narrative_style["findings"])
        issues.extend(specificity["findings"])
        issues.extend(hook["findings"])
        issues.extend(curiosity["findings"])
        issues.extend(storytelling["findings"])
        issues.extend(factuality["findings"])
        issues.extend(title["findings"])
        issues.extend(image["findings"])
        issues.extend(visual_interest["findings"])
        issues.extend(retention["findings"])
        issues.extend(blocking_failures)
        if av_delta > 0.5:
            issues.append(f"Audio/video duration mismatch is {av_delta:.2f}s")
        if any(image["source"] == "local_fallback" for image in image_probe):
            issues.append("One or more images used local fallback instead of Pollinations")
        return {
            "repeated_phrases": repeated_phrases,
            "specificity_score": round(specificity["score"], 1),
            "hook_score": round(hook["score"], 1),
            "curiosity_score": round(curiosity["score"], 1),
            "storytelling_score": round(storytelling["score"], 1),
            "narrative_naturalness_score": narrative_style["narrative_naturalness_score"],
            "disclaimer_leakage_score": narrative_style["disclaimer_leakage_score"],
            "coherence_score": round(coherence_score, 1),
            "factuality_risk_score": round(factuality["score"], 1),
            "generic_language_penalty": round(generic_analysis["penalty"], 1),
            "script_language": language_analysis["script_language"],
            "narration_language": language_analysis["narration_language"],
            "subtitle_language": language_analysis["subtitle_language"],
            "title_language": language_analysis["title_language"],
            "required_language": language_analysis["required_language"],
            "english_residue": language_analysis["english_residue"],
            "image_relevance_score": round(image["score"], 1),
            "visual_interest_score": round(visual_interest["score"], 1),
            "title_score": round(title["score"], 1),
            "freshness_score": freshness["score"],
            "trust_score": trust["score"],
            "final_quality_score": final_quality,
            "script_coherence_score": round(coherence_score, 1),
            "retention_score": round(retention["score"], 1),
            "estimated_viewer_retention_score": round(retention["score"], 1),
            "title_quality_score": round(title["score"], 1),
            "thumbnail_image_relevance_score": round(image["score"], 1),
            "audio_video_sync_score": round(av_sync, 1),
            "voice_naturalness_score": round(float(audio_probe.get("voice_naturalness_score", 0.0)), 1),
            "voice_engagement_score": round(float(audio_probe.get("voice_engagement_score", 0.0)), 1),
            "narration_pacing_score": round(float(audio_probe.get("narration_pacing_score", 0.0)), 1),
            "audio_video_duration_delta_seconds": round(av_delta, 2),
            "overall_quality_analysis_score": final_quality,
            "previous_structure_only_score": legacy_overall,
            "score_breakdown": {
                "specificity": specificity,
                "hook": hook,
                "curiosity": curiosity,
                "storytelling": storytelling,
                "narrative_style": narrative_style,
                "coherence": {"score": round(coherence_score, 1), "findings": []},
                "factuality_risk": factuality,
                "generic_language": generic_analysis,
                "language": language_analysis,
                "image_relevance": image,
                "visual_interest": visual_interest,
                "title": title,
                "freshness": freshness,
                "trust": trust,
                "retention": retention,
                "audio_video_sync": {"score": round(av_sync, 1), "findings": []},
                "voice_quality": {
                    "voice_naturalness_score": round(float(audio_probe.get("voice_naturalness_score", 0.0)), 1),
                    "voice_engagement_score": round(float(audio_probe.get("voice_engagement_score", 0.0)), 1),
                    "narration_pacing_score": round(float(audio_probe.get("narration_pacing_score", 0.0)), 1),
                    "voice_profile": audio_probe.get("voice_profile"),
                    "voice_name": audio_probe.get("voice_name"),
                    "voice_provider": audio_probe.get("voice_provider"),
                    "words_per_minute": audio_probe.get("words_per_minute"),
                    "pause_ratio": audio_probe.get("pause_ratio"),
                    "dynamic_variation": audio_probe.get("dynamic_variation"),
                    "findings": audio_probe.get("findings", []),
                },
            },
            "issues": issues,
            "blocking_failures": blocking_failures,
            "blocked": bool(blocking_failures),
            "verdict": "blocked" if blocking_failures else "good" if final_quality >= 80 and not issues else "needs_review",
        }

    def _repeated_phrases(self, words: list[str]) -> list[dict[str, Any]]:
        phrases = [" ".join(words[index : index + 3]) for index in range(max(0, len(words) - 2))]
        counts = Counter(phrase for phrase in phrases if len(phrase.split()) == 3)
        common = [
            {"phrase": phrase, "count": count}
            for phrase, count in counts.most_common(8)
            if count >= 3
        ]
        return common

    def _coherence_score(self, sentences: list[str]) -> float:
        if len(sentences) < 5:
            return 55.0
        connectors = ["because", "important", "signal", "takeaway", "expect", "context", "if", "porque", "importa", "pista", "revelação", "contexto", "se"]
        hits = sum(1 for sentence in sentences if any(word in sentence.lower() for word in connectors))
        score = min(90.0, 68.0 + hits * 3.5)
        if len(sentences) >= 7:
            score += 4
        return min(94.0, score)

    def _title_analysis(self, title: str, topic: TrendTopic) -> dict[str, Any]:
        length = len(title)
        lower = title.lower()
        score = 78.0
        findings: list[str] = []
        if length < 20:
            score -= 12
            findings.append("title is short and underspecified")
        if length > 90:
            score -= 20
            findings.append("title is too long for a Short")
        if re.search(r":\s*why it is trending\b", lower) or re.search(r":\s*por que", lower):
            score -= 28
            findings.append("generic title pattern detected: '[topic]: why it is trending'")
        elif ":" in title or "why" in lower:
            score += 4
        if not self._has_curiosity_gap(title):
            score -= 16
            findings.append("title does not create a specific curiosity gap")
        if title.count("!") > 1:
            score -= 18
            findings.append("title uses excessive punctuation")
        if len(self._topic_terms(topic.title) & self._token_set(title)) == 0:
            score -= 10
            findings.append("title does not clearly preserve topic keywords")
        return {"score": max(0.0, min(96.0, score)), "findings": findings}

    def _retention_analysis(
        self,
        script: ContentScript,
        repeated_phrases: list[dict[str, Any]],
        hook: dict[str, Any],
    ) -> dict[str, Any]:
        score = 70.0
        findings: list[str] = []
        narrative_style = analyze_reddit_narrative_style(script)
        hook_word_limit = 18 if narrative_style["reddit_prompt_present"] else 10
        if len(script.hook.split()) <= hook_word_limit:
            score += 9
        else:
            score -= 16
            findings.append("hook is too long for the first 2 seconds")
        if len(script.scenes) >= 8:
            score += 8
        else:
            score -= 14
            findings.append("too few scenes for visual pacing")
        score -= min(20, len(repeated_phrases) * 5)
        if repeated_phrases:
            findings.append("repeated phrases reduce retention quality")
        if hook["score"] < 65:
            score -= 12
            findings.append("weak hook reduces opening retention")
        if not self._has_payoff(script.narration):
            score -= 18
            findings.append("script does not deliver a clear payoff")
        if not self._has_clear_ending(script.narration):
            score -= 14
            findings.append("script lacks a clear ending")
        sentences = split_sentences(script.narration)
        if sentences:
            avg_words = sum(len(sentence.split()) for sentence in sentences) / len(sentences)
            if avg_words <= 16:
                score += 7
            else:
                score -= min(18, (avg_words - 16) * 2)
                findings.append(f"average sentence length is report-like ({avg_words:.1f} words)")
        lower = script.narration.lower()
        for label, terms in {
            "curiosity": ["clue", "hidden", "odd", "strange", "trap", "miss", "pista", "escond", "estranho", "armadilha", "quando", "até que"],
            "escalation": ["first", "then", "next", "worse", "primeiro", "depois", "em seguida", "quando", "de volta", "então", "mas"],
            "reveal": ["reveal", "payoff", "result", "revelação", "descobri", "acharam", "segurava", "não havia", "havia alguém", "esconderam", "avançou em mim"],
            "fast ending": ["remember", "watch this", "lembre", "só parei", "pedi demissão", "até hoje", "ali embaixo", "não existia", "cinquenta anos", "pneus", "mensagem", "muito antes"],
        }.items():
            if not any(term in lower for term in terms):
                score -= 7
                findings.append(f"missing {label} beat")
        return {"score": max(0.0, min(96.0, score)), "findings": findings}

    def _curiosity_analysis(self, script: ContentScript) -> dict[str, Any]:
        text = f"{script.title} {script.hook} {script.narration}".lower()
        findings: list[str] = []
        score = 74.0
        terms = [
            "clue",
            "hidden",
            "strange",
            "odd",
            "wrong",
            "missing",
            "before",
            "after",
            "twist",
            "trap",
            "mystery",
            "discovery",
            "pista",
            "escond",
            "estranho",
            "errado",
            "faltando",
            "antes",
            "depois",
            "virada",
            "armadilha",
            "mistério",
            "misterio",
            "descoberta",
            "quando",
            "até que",
        ]
        hits = [term for term in terms if term in text]
        score += min(20, len(hits) * 4)
        if len(script.hook.split()) <= 9:
            score += 4
        else:
            score -= 10
            findings.append("hook is too long for a two-second curiosity hit")
        if len(hits) < 2:
            score -= 18
            findings.append("script has too few curiosity anchors")
        if any(phrase in text for phrase in ["why it is trending", "here is what you need to know", "por que está em alta", "ganhando atenção"]):
            score -= 18
            findings.append("generic explainer language weakens curiosity")
        return {"score": max(0.0, min(96.0, score)), "findings": findings, "curiosity_terms": hits[:8]}

    def _storytelling_analysis(self, script: ContentScript) -> dict[str, Any]:
        style = analyze_reddit_narrative_style(script)
        if style["reddit_prompt_present"] and (
            style["first_person_signal_count"] >= 3 or style["close_third_person_present"]
        ):
            score = 92.0
            findings = list(style["findings"])
            if style["average_sentence_words"] > 15:
                score -= min(18, (style["average_sentence_words"] - 15) * 2)
            return {
                "score": max(0.0, min(96.0, score)),
                "findings": findings,
                "beats_present": 5,
            }
        lower = script.narration.lower()
        findings: list[str] = []
        score = 72.0
        beats = {
            "hook": [script.hook.lower()],
            "curiosity": ["curiosity", "clue", "hidden", "strange", "odd", "missing", "curiosidade", "pista", "escond", "estranho", "faltando"],
            "escalation": ["first", "then", "next", "gets sharper", "worse", "raises the stakes", "stacking", "primeiro", "depois", "em seguida", "pior", "tensão"],
            "reveal": ["reveal", "payoff", "result", "changed", "turns", "revelação", "recompensa", "resultado", "mudou", "virada"],
            "ending": ["fast ending", "remember", "ending", "watch", "final rápido", "lembre", "final", "veja"],
        }
        present = 0
        for label, terms in beats.items():
            if any(term and term in lower for term in terms):
                present += 1
            else:
                findings.append(f"missing story structure beat: {label}")
        score += present * 5
        sentences = split_sentences(script.narration)
        if sentences:
            avg_words = sum(len(sentence.split()) for sentence in sentences) / len(sentences)
            if avg_words <= 15:
                score += 5
            else:
                score -= min(18, (avg_words - 15) * 2)
                findings.append(f"average story sentence length is {avg_words:.1f} words")
        if len(script.scenes) >= 8:
            score += 4
        else:
            score -= 12
            findings.append("story has too few visual/subtitle beats")
        return {"score": max(0.0, min(96.0, score)), "findings": findings, "beats_present": present}

    def _visual_interest_analysis(
        self,
        script: ContentScript,
        topic: TrendTopic,
        video_probe: dict[str, Any],
        image_probe: list[dict[str, Any]],
    ) -> dict[str, Any]:
        findings: list[str] = []
        scene_count = len(script.scenes)
        image_count = len(image_probe)
        duration = float(video_probe.get("duration_seconds", 0.0) or 0.0)
        background_mode = str(video_probe.get("background_mode", ""))
        if background_mode in {"retention_categories", "custom_background_library"} or any(
            image.get("source") in {"procedural_retention_background", "custom_background_library"}
            for image in image_probe
        ):
            score = 90.0
            if scene_count >= 8:
                score += min(8, (scene_count - 7) * 2)
            else:
                score -= 18
                findings.append(f"only {scene_count} subtitle beats; story pacing needs at least 8")
            if not video_probe.get("subtitles_enabled"):
                score -= 35
                findings.append("story mode subtitles are not enabled")
            if not video_probe.get("background_animated", True):
                score -= 35
                findings.append("retention background is not animated")
            if background_mode == "retention_categories" and not bool(
                video_probe.get("background_looped", True)
            ) and duration > float(
                self.config.get("story_mode", {}).get("background_video", {}).get("loop_seconds", 8.0)
            ):
                score -= 16
                findings.append("retention background did not loop for long narration")
            return {
                "score": max(0.0, min(96.0, score)),
                "findings": findings,
                "scene_count": scene_count,
                "image_count": image_count,
                "background_mode": background_mode,
                "background_category": video_probe.get("background_category"),
                "background_tier": video_probe.get("background_tier"),
                "background_filename": video_probe.get("background_filename"),
                "background_animated": bool(video_probe.get("background_animated", True)),
                "royalty_free": True,
                "subtitles_enabled": bool(video_probe.get("subtitles_enabled")),
                "looped": bool(video_probe.get("background_looped")),
                "estimated_visual_change_interval_seconds": 1.0,
            }
        config = getattr(self, "config", {}) or {}
        configured_beat = float(
            config.get("generation", {}).get("video", {}).get("max_visual_beat_seconds", 2.0)
        )
        seconds_per_image = duration / max(1, image_count)
        effective_change_interval = min(seconds_per_image, configured_beat)
        score = 76.0
        if scene_count >= 8:
            score += min(10, (scene_count - 7) * 2)
        else:
            score -= 24
            findings.append(f"only {scene_count} scenes; Shorts pacing needs at least 8")
        if effective_change_interval <= 2.05:
            score += 8
        else:
            score -= 25
            findings.append(f"visual change interval is too slow ({effective_change_interval:.2f}s)")
        topic_terms = self._topic_terms(topic.title)
        generic_terms = {
            "abstract",
            "gradient",
            "background",
            "title",
            "slide",
            "text",
            "visualization",
            "trend",
            "newsroom",
            "creator",
            "studio",
            "abstrato",
            "gradiente",
            "fundo",
            "texto",
            "titulo",
            "título",
            "slide",
            "dados",
            "visualizacao",
            "visualização",
        }
        long_caption_count = 0
        generic_prompt_count = 0
        relevant_prompt_count = 0
        for scene in script.scenes:
            caption = clean_text(scene.get("caption", ""))
            prompt = clean_text(scene.get("image_prompt", ""))
            prompt_terms = self._token_set(prompt)
            if len(caption.split()) > 6 or len(caption) > 42:
                long_caption_count += 1
            if topic_terms and topic_terms & prompt_terms:
                relevant_prompt_count += 1
            if len(generic_terms & prompt_terms) >= 3 or "static gradient" in prompt.lower() or "text-only" in prompt.lower():
                generic_prompt_count += 1
        if long_caption_count:
            score -= min(18, long_caption_count * 4)
            findings.append("on-screen text is too long for retention")
        if scene_count and relevant_prompt_count / scene_count < 0.65:
            score -= 18
            findings.append("not enough scene prompts are topic-specific")
        if generic_prompt_count:
            score -= min(30, generic_prompt_count * 7)
            findings.append("some visuals look like generic slides or backgrounds")
        if image_count < scene_count:
            score -= 12
            findings.append("not every scene has a generated image")
        if any(image.get("source") == "local_fallback" for image in image_probe):
            score -= 8
            findings.append("local fallback images reduce visual interest")
        return {
            "score": max(0.0, min(96.0, score)),
            "findings": findings,
            "scene_count": scene_count,
            "image_count": image_count,
            "configured_max_visual_beat_seconds": configured_beat,
            "estimated_visual_change_interval_seconds": round(effective_change_interval, 2),
            "topic_specific_scene_count": relevant_prompt_count,
            "generic_prompt_count": generic_prompt_count,
            "long_caption_count": long_caption_count,
        }

    def _image_relevance_analysis(
        self,
        script: ContentScript,
        topic: TrendTopic,
        image_probe: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if any(
            image.get("source") in {"procedural_retention_background", "custom_background_library"}
            for image in image_probe
        ):
            background = next(
                (
                    image
                    for image in image_probe
                    if image.get("source") in {"procedural_retention_background", "custom_background_library"}
                ),
                {},
            )
            custom = background.get("source") == "custom_background_library"
            return {
                "score": 92.0,
                "findings": [
                    "story mode uses a user-approved custom background"
                    if custom
                    else "story mode uses a procedural royalty-free approved retention background"
                ],
                "scene_scores": [90.0],
                "background_mode": "custom_background_library" if custom else "retention_categories",
                "background_category": background.get("background_category"),
                "background_tier": background.get("background_tier"),
                "background_filename": background.get("background_filename"),
                "animated": bool(background.get("animated", True)),
                "royalty_free": True,
            }
        topic_terms = self._topic_terms(topic.title)
        script_terms = self._topic_terms(script.narration)
        generic_visual_terms = {
            "abstract",
            "data",
            "visualization",
            "trend",
            "trending",
            "social",
            "media",
            "modern",
            "newsroom",
            "creator",
            "studio",
            "phone",
            "screens",
            "comments",
            "editorial",
            "cinematic",
            "abstrato",
            "gradiente",
            "fundo",
            "texto",
            "tendência",
            "tendencia",
        }
        concrete_visual_terms = {
            "fifa",
            "world",
            "cup",
            "football",
            "calendar",
            "fixture",
            "fixtures",
            "group",
            "stage",
            "round",
            "quarterfinal",
            "semifinal",
            "final",
            "stadium",
            "host",
            "city",
            "teams",
            "match",
            "june",
            "2026",
        }
        findings: list[str] = []
        if not topic_terms:
            return {"score": 58.0, "findings": ["image relevance cannot be proven without topic keywords"]}
        scene_scores = []
        for scene in script.scenes:
            prompt = scene.get("image_prompt", "")
            prompt_terms = self._token_set(prompt)
            topic_overlap = len(topic_terms & prompt_terms)
            script_overlap = len((script_terms - generic_visual_terms) & prompt_terms)
            concrete_overlap = len(concrete_visual_terms & prompt_terms)
            generic_count = len(generic_visual_terms & prompt_terms)
            score = (
                42
                + min(30, topic_overlap * 12)
                + min(24, script_overlap * 4)
                + min(24, concrete_overlap * 6)
                - min(18, generic_count * 2)
            )
            scene_scores.append(max(15.0, min(96.0, score)))
        score = sum(scene_scores) / max(1, len(scene_scores))
        if any(image["source"] == "local_fallback" for image in image_probe):
            score -= 8
            findings.append("local fallback images reduce visual evidence")
        if score < 70:
            findings.append("image prompts rely on generic background concepts more than topic details")
        return {"score": max(0.0, min(96.0, score)), "findings": findings, "scene_scores": [round(v, 1) for v in scene_scores]}

    def _generic_language_analysis(self, script: ContentScript) -> dict[str, Any]:
        text = f"{script.title} {script.hook} {script.description} {script.narration}".lower()
        phrase_weights = {
            "why it is trending": 12,
            "gaining attention": 8,
            "people are talking about": 8,
            "here is what you need to know": 10,
            "this is important": 8,
            "suddenly everywhere": 8,
            "signal worth watching": 7,
            "the important part": 6,
            "the takeaway": 5,
            "por que está em alta": 12,
            "ganhando atenção": 8,
            "todo mundo está falando": 8,
            "isso é importante": 8,
        }
        findings: list[str] = []
        penalty = 0.0
        hits: list[dict[str, Any]] = []
        for phrase, weight in phrase_weights.items():
            count = text.count(phrase)
            if count:
                amount = weight * count
                penalty += amount
                hits.append({"phrase": phrase, "count": count, "penalty": amount})
                findings.append(f"generic phrase detected: '{phrase}'")
        vague_words = {"trend", "topic", "attention", "conversation", "context", "signal", "questions", "reactions", "tendência", "tendencia", "tema", "atenção", "atencao", "conversa", "sinal", "perguntas", "reações", "reacoes"}
        vague_count = sum(1 for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ']+", text) if word in vague_words)
        if vague_count > 14:
            vague_penalty = min(14.0, (vague_count - 14) * 1.5)
            penalty += vague_penalty
            findings.append(f"vague trend-language density is high ({vague_count} terms)")
        return {
            "penalty": min(40.0, penalty),
            "findings": findings,
            "phrase_hits": hits,
            "vague_term_count": vague_count,
        }

    def _specificity_analysis(
        self,
        script: ContentScript,
        topic: TrendTopic,
        research: ResearchBrief | None = None,
    ) -> dict[str, Any]:
        text = f"{script.title} {script.description} {script.narration}"
        lower = text.lower()
        topic_lower = topic.title.lower()
        findings: list[str] = []
        details: list[str] = []
        month_re = r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\b"
        if re.search(month_re, lower) or re.search(r"\b(19|20)\d{2}\b", lower) or re.search(r"\b\d{1,2}/\d{1,2}\b", lower):
            details.append("date_or_year")
        if re.search(r"\b(final|semifinal|quarterfinal|group stage|knockout|qualifier|matchday|fixture|draw|quartas|oitavas|fase de grupos|eliminatória|eliminatoria|jogo marcado|calendário|calendario)\b", lower):
            details.append("stage_or_match_context")
        if re.search(r"\b(fifa|official|federation|organizer|schedule release|match calendar|oficial|federação|federacao|organizador|calendário de jogos|calendario de jogos)\b", lower):
            details.append("official_context")
        if re.search(r"\b(united states|usa|canada|mexico|qatar|brazil|argentina|england|spain|france|germany|portugal|italy|teams?|host country|hosts?|estados unidos|canadá|canada|méxico|mexico|brasil|argentina|inglaterra|espanha|frança|franca|alemanha|itália|italia|times?|sede|sedes)\b", lower):
            details.append("team_or_host_country")

        named_entities = self._named_entities(text)
        topic_terms = self._topic_terms(topic.title)
        specific_entities = [
            entity for entity in named_entities
            if not self._topic_terms(entity).issubset(topic_terms)
        ]
        if specific_entities:
            details.append("named_entity")

        numbers = re.findall(r"\b\d{1,4}\b", text)
        if len(numbers) >= 2:
            details.append("multiple_numbers")

        unique_details = sorted(set(details))
        detail_count = len(unique_details)
        used_research_facts = self._used_research_facts(text, research) if research else []
        if "world cup" in topic_lower or "schedule" in topic_lower or "copa do mundo" in topic_lower or "calendário" in topic_lower or "calendario" in topic_lower:
            required_score = {0: 18, 1: 38, 2: 62}.get(detail_count, 88)
            if detail_count < 3:
                findings.append(
                    "topic lacks at least 3 concrete schedule details such as dates, teams, host country, stage, or official context"
                )
        else:
            required_score = min(92, 28 + detail_count * 18)
            if detail_count < 3:
                findings.append("script includes fewer than 3 concrete topic details")
        if len(used_research_facts) >= 3:
            required_score = max(required_score, 86 + min(8, len(used_research_facts) - 3))
            findings = [
                finding for finding in findings
                if "fewer than 3 concrete" not in finding and "lacks at least 3" not in finding
            ]
        elif research:
            findings.append(f"script uses only {len(used_research_facts)}/3 required research facts")
        return {
            "score": float(required_score),
            "findings": findings,
            "detail_count": detail_count,
            "details": unique_details,
            "named_entities": specific_entities[:8],
            "used_research_facts": used_research_facts,
        }

    def _used_research_facts(self, text: str, research: ResearchBrief | None) -> list[str]:
        if not research:
            return []
        text_terms = self._token_set(text)
        used: list[str] = []
        for fact in research.concrete_facts:
            fact_terms = {term for term in self._token_set(fact) if len(term) > 4}
            if not fact_terms:
                continue
            overlap = len(text_terms & fact_terms)
            if overlap >= min(3, len(fact_terms)):
                used.append(fact)
        return used[:7]

    def _hook_analysis(self, script: ContentScript, topic: TrendTopic) -> dict[str, Any]:
        hook = clean_text(script.hook)
        lower = hook.lower()
        score = 76.0
        findings: list[str] = []
        if is_reddit_story_topic(topic) and "reddit" in lower and "?" in hook:
            score = 94.0
            if len(hook.split()) > 18:
                score -= min(24, (len(hook.split()) - 18) * 3)
                findings.append("Reddit prompt is too long for the opening")
            return {"score": max(0.0, min(94.0, score)), "findings": findings}
        if len(hook.split()) > 14:
            score -= 14
            findings.append("hook is too long for a strong Shorts open")
        if any(phrase in lower for phrase in ["suddenly everywhere", "gaining attention", "people are talking", "ganhando atenção", "todo mundo está falando"]):
            score -= 30
            findings.append("hook relies on generic trend language")
        if len(self._topic_terms(topic.title) & self._token_set(hook)) == 0:
            score -= 10
            findings.append("hook does not include topic keywords")
        if not self._has_curiosity_gap(hook):
            score -= 18
            findings.append("hook has no specific curiosity gap")
        return {"score": max(0.0, min(94.0, score)), "findings": findings}

    def _factuality_risk_analysis(self, script: ContentScript, specificity: dict[str, Any]) -> dict[str, Any]:
        detail_count = int(specificity.get("detail_count", 0))
        score = 94.0 if detail_count >= 4 else 82.0
        findings: list[str] = []
        if detail_count == 0:
            score -= 35
            findings.append("high factuality risk: script makes explanatory claims without concrete facts")
        elif detail_count < 3:
            score -= 18
            findings.append("moderate factuality risk: fewer than 3 concrete details")
        hedge_terms = ["usually", "expect", "if this keeps", "worth watching", "the smart take", "geralmente", "se isso continuar", "vale observar"]
        hedge_count = sum(script.narration.lower().count(term) for term in hedge_terms)
        if hedge_count >= 3:
            score -= 10
            findings.append("script leans on hedged generic claims rather than verifiable facts")
        return {"score": max(0.0, min(95.0, score)), "findings": findings}

    def _has_curiosity_gap(self, text: str) -> bool:
        lower = text.lower()
        concrete_gap_terms = [
            "why", "how", "what changed", "hidden", "before", "after", "reason", "schedule", "date", "teams", "final", "clue", "twist", "trap", "miss",
            "por que", "como", "o que mudou", "escond", "antes", "depois", "motivo", "calendário", "calendario", "data", "times", "pista", "virada", "armadilha", "perde",
        ]
        return any(term in lower for term in concrete_gap_terms) and not lower.endswith("why it is trending")

    def _has_payoff(self, narration: str) -> bool:
        lower = narration.lower()
        payoff_terms = [
            "the reveal",
            "the payoff",
            "the result",
            "a revelação",
            "o resultado",
            "quando saí",
            "quando entraram",
            "descobri",
            "acharam",
            "segurava uma",
            "não havia ninguém",
            "havia alguém",
            "ela tinha a mesma",
            "meu próprio carro",
            "nos esconderam",
            "tentou pular",
            "avançou em mim",
            "barricada",
        ]
        return any(term in lower for term in payoff_terms)

    def _has_clear_ending(self, narration: str) -> bool:
        sentences = split_sentences(narration)
        if not sentences:
            return False
        ending = sentences[-1].lower()
        return any(
            term in ending
            for term in [
                "watch",
                "remember",
                "clue",
                "matters",
                "veja",
                "lembre",
                "só parei",
                "pedi demissão",
                "ali embaixo",
                "até hoje",
                "cinquenta anos",
                "não existia",
                "pneus",
                "cidade",
                "mensagem",
                "muito antes",
            ]
        )

    def _topic_terms(self, text: str) -> set[str]:
        stopwords = {
            "the", "and", "for", "with", "from", "that", "this", "what", "why", "how", "are", "was",
            "were", "is", "it", "its", "into", "about", "schedule", "trend", "trending",
            "que", "para", "com", "uma", "por", "como", "isso", "este", "esta", "calendário", "calendario", "tendência", "tendencia",
        }
        return {word for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", text.lower()) if len(word) > 2 and word not in stopwords}

    def _token_set(self, text: str) -> set[str]:
        return {word for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", text.lower()) if len(word) > 2}

    def _named_entities(self, text: str) -> list[str]:
        candidates = re.findall(r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]+(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]+){0,3}\b", text)
        banned = {"The", "If", "When", "Here", "This", "That", "ShortsMaster"}
        return [candidate for candidate in candidates if candidate.split()[0] not in banned]

    def _metadata(
        self,
        selected: TrendTopic,
        script: ContentScript,
        queue_id: int,
        upload_id: str,
        voice_path: Path,
        image_paths: list[Path],
        video_path: Path,
        content_decision: dict[str, Any],
        upload_decision: dict[str, Any],
        audio_probe: dict[str, Any],
        video_probe: dict[str, Any],
        image_probe: list[dict[str, Any]],
        quality_analysis: dict[str, Any],
        research: ResearchBrief,
        story_source_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        queue_item = self.pipeline.db.get_queue_item(queue_id) or {}
        warnings = list(self.warnings)
        warnings.extend(self.pipeline.scripts.last_warnings)
        warnings.extend(self.pipeline.images.last_warnings)
        warnings.extend(content_decision.get("warnings", []))
        warnings.extend(upload_decision.get("warnings", []))
        warnings.extend(quality_analysis.get("issues", []))
        return {
            "validation_timestamp": utc_now_iso(),
            "paper_mode": bool(self.config.get("app", {}).get("paper_mode", True)),
            "real_upload_enabled": bool(self.config.get("publishing", {}).get("enable_real_upload", False)),
            "selected_topic": selected.title,
            "trend_source": selected.source,
            "trend_score": selected.score,
            "niche": selected.niche,
            "generated_title": script.title,
            "generated_description": script.description,
            "generated_hashtags": script.tags,
            "script_language": quality_analysis.get("script_language"),
            "narration_language": quality_analysis.get("narration_language"),
            "subtitle_language": quality_analysis.get("subtitle_language"),
            "title_language": quality_analysis.get("title_language"),
            "research_brief": research.to_dict(),
            "research_fact_count": len(research.concrete_facts),
            "research_sources": research.source_urls,
            "source_dates": research.source_dates,
            "freshness_score": round(float(research.freshness_score), 1),
            "freshness_threshold": round(float(research.freshness_threshold), 1),
            "freshness_required": bool(research.requires_fresh_source),
            "freshness_window_hours": int(research.freshness_window_hours),
            "freshness_notes": research.freshness_notes,
            "trust_score": round(float(research.trust_score), 1),
            "trust_threshold": round(float(research.trust_threshold), 1),
            "trust_required": bool(research.requires_trusted_source),
            "trust_notes": research.trust_notes,
            "script_word_count": len(script.narration.split()),
            "script_provider": self.pipeline.scripts.last_provider,
            "audio_duration_seconds": audio_probe["duration_seconds"],
            "voice_profile": audio_probe.get("voice_profile"),
            "voice_name": audio_probe.get("voice_name"),
            "voice_provider": audio_probe.get("voice_provider"),
            "voice_naturalness_score": quality_analysis.get("voice_naturalness_score"),
            "voice_engagement_score": quality_analysis.get("voice_engagement_score"),
            "narration_pacing_score": quality_analysis.get("narration_pacing_score"),
            "video_duration_seconds": video_probe["duration_seconds"],
            "video_resolution": f"{video_probe['width']}x{video_probe['height']}",
            "final_file_size_bytes": video_probe["file_size_bytes"],
            "quality_score": quality_analysis["final_quality_score"],
            "hook_score": quality_analysis.get("hook_score"),
            "curiosity_score": quality_analysis.get("curiosity_score"),
            "storytelling_score": quality_analysis.get("storytelling_score"),
            "narrative_naturalness_score": quality_analysis.get("narrative_naturalness_score"),
            "disclaimer_leakage_score": quality_analysis.get("disclaimer_leakage_score"),
            "retention_score": quality_analysis.get("retention_score"),
            "visual_interest_score": quality_analysis.get("visual_interest_score"),
            "safety_score": content_decision.get("safety_score", content_decision["quality_score"]),
            "story_source_report": story_source_report,
            "story_source_url": (story_source_report or {}).get("source_url"),
            "background_video_mode": video_probe.get("background_mode"),
            "background_category": video_probe.get("background_category"),
            "background_tier": video_probe.get("background_tier"),
            "background_filename": video_probe.get("background_filename"),
            "background_source_url": video_probe.get("background_source_url"),
            "background_license_type": video_probe.get("background_license_type"),
            "background_commercial_rights_verified": video_probe.get(
                "background_commercial_rights_verified"
            ),
            "background_animated": video_probe.get("background_animated"),
            "subtitles_enabled": video_probe.get("subtitles_enabled"),
            "background_looped": video_probe.get("background_looped"),
            "background_trimmed": video_probe.get("background_trimmed"),
            "safety_decision": content_decision,
            "queue_id": queue_id,
            "queue_status": queue_item.get("status"),
            "upload_simulation_result": upload_id,
            "upload_decision": upload_decision,
            "audio": audio_probe,
            "video": video_probe,
            "images": image_probe,
            "quality_analysis": quality_analysis,
            "warnings": warnings,
            "failures": self.failures,
            "source_artifacts": {
                "final_mp4": str(video_path),
                "generated_audio": str(voice_path),
                "generated_images": [str(path) for path in image_paths],
            },
        }

    def _copy_artifacts(
        self,
        voice_path: Path,
        image_paths: list[Path],
        video_path: Path,
        script_path: Path,
        research_path: Path,
        story_source_path: Path | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        copied_video = self.artifact_dir / "final_short.mp4"
        copied_audio = self.artifact_dir / voice_path.name
        shutil.copy2(video_path, copied_video)
        shutil.copy2(voice_path, copied_audio)
        copied_images = []
        image_dir = self.artifact_dir / "images"
        image_dir.mkdir(exist_ok=True)
        for path in image_paths:
            target = image_dir / path.name
            shutil.copy2(path, target)
            copied_images.append(str(target))
        copied_script = self.artifact_dir / "generated_script.json"
        if script_path != copied_script:
            shutil.copy2(script_path, copied_script)
        copied_research = self.artifact_dir / "research_brief.json"
        if research_path != copied_research:
            shutil.copy2(research_path, copied_research)
        artifacts = {
            "final_mp4": str(copied_video),
            "generated_audio": str(copied_audio),
            "generated_images": copied_images,
            "generated_script": str(copied_script),
            "research_brief": str(copied_research),
        }
        if story_source_path and story_source_path.exists():
            copied_story_source = self.artifact_dir / "story_source_report.json"
            if story_source_path != copied_story_source:
                shutil.copy2(story_source_path, copied_story_source)
            artifacts["story_source_report"] = str(copied_story_source)
        checklist_paths = self._copy_pre_upload_checklist()
        artifacts.update(checklist_paths)
        return artifacts

    def _copy_pre_upload_checklist(self) -> dict[str, str]:
        reports_dir = resolve_storage_path(self.config, self.config.get("storage", {}).get("reports_dir", "reports"))
        artifacts: dict[str, str] = {}
        for name, key in [
            ("youtube_pre_upload_checklist.json", "youtube_pre_upload_checklist_json"),
            ("youtube_pre_upload_checklist.html", "youtube_pre_upload_checklist_html"),
        ]:
            source = reports_dir / name
            if source.exists():
                target = self.artifact_dir / name
                shutil.copy2(source, target)
                artifacts[key] = str(target)
        return artifacts

    def _write_category_performance_report(self) -> dict[str, str]:
        background_config = self.config.get("story_mode", {}).get("background_video", {})
        payload = {
            "generated_at": utc_now_iso(),
            "background_mode": background_config.get("mode", "custom_background_library"),
            "approved_categories": background_config.get(
                "approved_categories",
                {
                    "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
                    "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
                },
            ),
            "category_performance": self.pipeline.db.background_category_performance(),
        }
        reports_dir = resolve_storage_path(self.config, self.config.get("storage", {}).get("reports_dir", "reports"))
        reports_dir.mkdir(parents=True, exist_ok=True)
        reports_path = reports_dir / "category_performance_report.json"
        artifact_path = self.artifact_dir / "category_performance_report.json"
        reports_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        artifact_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        return {
            "category_performance_report_json": str(artifact_path),
            "global_category_performance_report_json": str(reports_path),
        }

    def _write_background_library_report(self) -> dict[str, str]:
        paths = self.pipeline.background_library.write_report()
        artifacts: dict[str, str] = {
            "global_background_library_report_json": paths["json"],
            "global_background_library_report_html": paths["html"],
        }
        for source_key, artifact_key in [
            ("json", "background_library_report_json"),
            ("html", "background_library_report_html"),
        ]:
            source = Path(paths[source_key])
            target = self.artifact_dir / source.name
            shutil.copy2(source, target)
            artifacts[artifact_key] = str(target)
        return artifacts

    def _needs_research_report(
        self,
        selected: TrendTopic,
        queue_id: int,
        research: ResearchBrief,
        reason: str,
        research_path: Path,
    ) -> dict[str, Any]:
        metadata = {
            "validation_timestamp": utc_now_iso(),
            "paper_mode": bool(self.config.get("app", {}).get("paper_mode", True)),
            "real_upload_enabled": bool(self.config.get("publishing", {}).get("enable_real_upload", False)),
            "selected_topic": selected.title,
            "trend_source": selected.source,
            "trend_score": selected.score,
            "niche": selected.niche,
            "script_language": "not_generated",
            "narration_language": "not_generated",
            "subtitle_language": "not_generated",
            "title_language": "not_generated",
            "research_brief": research.to_dict(),
            "research_fact_count": len(research.concrete_facts),
            "research_sources": research.source_urls,
            "source_dates": research.source_dates,
            "freshness_score": round(float(research.freshness_score), 1),
            "freshness_threshold": round(float(research.freshness_threshold), 1),
            "freshness_required": bool(research.requires_fresh_source),
            "freshness_window_hours": int(research.freshness_window_hours),
            "freshness_notes": research.freshness_notes,
            "trust_score": round(float(research.trust_score), 1),
            "trust_threshold": round(float(research.trust_threshold), 1),
            "trust_required": bool(research.requires_trusted_source),
            "trust_notes": research.trust_notes,
            "queue_id": queue_id,
            "queue_status": QueueStatus.NEEDS_RESEARCH,
            "quality_score": 0.0,
            "safety_score": None,
            "upload_simulation_result": None,
            "warnings": [*self.warnings, reason, *research.uncertainty_notes],
            "failures": [],
            "quality_analysis": {
                "specificity_score": 0.0,
                "hook_score": 0.0,
                "curiosity_score": 0.0,
                "storytelling_score": 0.0,
                "retention_score": 0.0,
                "visual_interest_score": 0.0,
                "coherence_score": 0.0,
                "factuality_risk_score": 0.0,
                "generic_language_penalty": 0.0,
                "image_relevance_score": 0.0,
                "title_score": 0.0,
                "freshness_score": round(float(research.freshness_score), 1),
                "trust_score": round(float(research.trust_score), 1),
                "final_quality_score": 0.0,
                "issues": [reason],
                "verdict": "needs_research",
            },
            "artifacts": {
                "research_brief": str(research_path),
            },
        }
        if self.story_source_report_path and self.story_source_report_path.exists():
            metadata["artifacts"]["story_source_report"] = str(self.story_source_report_path)
        metadata_path = self.artifact_dir / "metadata.json"
        report_path = self.artifact_dir / "validation_report.json"
        html_path = self.artifact_dir / "validation_report.html"
        metadata["artifacts"]["metadata_json"] = str(metadata_path)
        metadata["artifacts"]["validation_report_json"] = str(report_path)
        metadata["artifacts"]["validation_report_html"] = str(html_path)
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        report_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        html_path.write_text(self._html_report(metadata), encoding="utf-8")
        return metadata

    def _needs_fresh_source_report(
        self,
        selected: TrendTopic,
        queue_id: int,
        research: ResearchBrief,
        reason: str,
        research_path: Path,
    ) -> dict[str, Any]:
        metadata = {
            "validation_timestamp": utc_now_iso(),
            "paper_mode": bool(self.config.get("app", {}).get("paper_mode", True)),
            "real_upload_enabled": bool(self.config.get("publishing", {}).get("enable_real_upload", False)),
            "selected_topic": selected.title,
            "trend_source": selected.source,
            "trend_score": selected.score,
            "niche": selected.niche,
            "script_language": "not_generated",
            "narration_language": "not_generated",
            "subtitle_language": "not_generated",
            "title_language": "not_generated",
            "research_brief": research.to_dict(),
            "research_fact_count": len(research.concrete_facts),
            "research_sources": research.source_urls,
            "source_dates": research.source_dates,
            "freshness_score": round(float(research.freshness_score), 1),
            "freshness_threshold": round(float(research.freshness_threshold), 1),
            "freshness_required": bool(research.requires_fresh_source),
            "freshness_window_hours": int(research.freshness_window_hours),
            "freshness_notes": research.freshness_notes,
            "trust_score": round(float(research.trust_score), 1),
            "trust_threshold": round(float(research.trust_threshold), 1),
            "trust_required": bool(research.requires_trusted_source),
            "trust_notes": research.trust_notes,
            "queue_id": queue_id,
            "queue_status": QueueStatus.NEEDS_FRESH_SOURCE,
            "quality_score": 0.0,
            "safety_score": None,
            "upload_simulation_result": None,
            "warnings": [*self.warnings, reason, *research.freshness_notes],
            "failures": [],
            "quality_analysis": {
                "specificity_score": 0.0,
                "hook_score": 0.0,
                "curiosity_score": 0.0,
                "storytelling_score": 0.0,
                "retention_score": 0.0,
                "visual_interest_score": 0.0,
                "coherence_score": 0.0,
                "factuality_risk_score": 0.0,
                "generic_language_penalty": 0.0,
                "image_relevance_score": 0.0,
                "title_score": 0.0,
                "freshness_score": round(float(research.freshness_score), 1),
                "trust_score": round(float(research.trust_score), 1),
                "final_quality_score": 0.0,
                "issues": [reason],
                "verdict": "needs_fresh_source",
            },
            "artifacts": {
                "research_brief": str(research_path),
            },
        }
        if self.story_source_report_path and self.story_source_report_path.exists():
            metadata["artifacts"]["story_source_report"] = str(self.story_source_report_path)
        metadata_path = self.artifact_dir / "metadata.json"
        report_path = self.artifact_dir / "validation_report.json"
        html_path = self.artifact_dir / "validation_report.html"
        metadata["artifacts"]["metadata_json"] = str(metadata_path)
        metadata["artifacts"]["validation_report_json"] = str(report_path)
        metadata["artifacts"]["validation_report_html"] = str(html_path)
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        report_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        html_path.write_text(self._html_report(metadata), encoding="utf-8")
        return metadata

    def _needs_trusted_source_report(
        self,
        selected: TrendTopic,
        queue_id: int,
        research: ResearchBrief,
        reason: str,
        research_path: Path,
    ) -> dict[str, Any]:
        metadata = {
            "validation_timestamp": utc_now_iso(),
            "paper_mode": bool(self.config.get("app", {}).get("paper_mode", True)),
            "real_upload_enabled": bool(self.config.get("publishing", {}).get("enable_real_upload", False)),
            "selected_topic": selected.title,
            "trend_source": selected.source,
            "trend_score": selected.score,
            "niche": selected.niche,
            "script_language": "not_generated",
            "narration_language": "not_generated",
            "subtitle_language": "not_generated",
            "title_language": "not_generated",
            "research_brief": research.to_dict(),
            "research_fact_count": len(research.concrete_facts),
            "research_sources": research.source_urls,
            "source_dates": research.source_dates,
            "freshness_score": round(float(research.freshness_score), 1),
            "freshness_threshold": round(float(research.freshness_threshold), 1),
            "freshness_required": bool(research.requires_fresh_source),
            "freshness_window_hours": int(research.freshness_window_hours),
            "freshness_notes": research.freshness_notes,
            "trust_score": round(float(research.trust_score), 1),
            "trust_threshold": round(float(research.trust_threshold), 1),
            "trust_required": bool(research.requires_trusted_source),
            "trust_notes": research.trust_notes,
            "queue_id": queue_id,
            "queue_status": QueueStatus.NEEDS_TRUSTED_SOURCE,
            "quality_score": 0.0,
            "safety_score": None,
            "upload_simulation_result": None,
            "warnings": [*self.warnings, reason, *research.trust_notes],
            "failures": [],
            "quality_analysis": {
                "specificity_score": 0.0,
                "hook_score": 0.0,
                "curiosity_score": 0.0,
                "storytelling_score": 0.0,
                "retention_score": 0.0,
                "visual_interest_score": 0.0,
                "coherence_score": 0.0,
                "factuality_risk_score": 0.0,
                "generic_language_penalty": 0.0,
                "image_relevance_score": 0.0,
                "title_score": 0.0,
                "freshness_score": round(float(research.freshness_score), 1),
                "trust_score": round(float(research.trust_score), 1),
                "final_quality_score": 0.0,
                "issues": [reason],
                "verdict": "needs_trusted_source",
            },
            "artifacts": {
                "research_brief": str(research_path),
            },
        }
        if self.story_source_report_path and self.story_source_report_path.exists():
            metadata["artifacts"]["story_source_report"] = str(self.story_source_report_path)
        metadata_path = self.artifact_dir / "metadata.json"
        report_path = self.artifact_dir / "validation_report.json"
        html_path = self.artifact_dir / "validation_report.html"
        metadata["artifacts"]["metadata_json"] = str(metadata_path)
        metadata["artifacts"]["validation_report_json"] = str(report_path)
        metadata["artifacts"]["validation_report_html"] = str(html_path)
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        report_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        html_path.write_text(self._html_report(metadata), encoding="utf-8")
        return metadata

    def _html_report(self, metadata: dict[str, Any]) -> str:
        rows = []
        keys = [
            "selected_topic",
            "trend_score",
            "generated_title",
            "generated_description",
            "generated_hashtags",
            "script_language",
            "narration_language",
            "subtitle_language",
            "title_language",
            "script_word_count",
            "audio_duration_seconds",
            "video_duration_seconds",
            "video_resolution",
            "final_file_size_bytes",
            "quality_score",
            "hook_score",
            "curiosity_score",
            "storytelling_score",
            "voice_profile",
            "voice_name",
            "voice_provider",
            "voice_naturalness_score",
            "voice_engagement_score",
            "narration_pacing_score",
            "retention_score",
            "visual_interest_score",
            "safety_score",
            "freshness_score",
            "trust_score",
            "background_video_mode",
            "background_category",
            "background_animated",
            "subtitles_enabled",
            "queue_status",
            "upload_simulation_result",
        ]
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=True)
            rows.append(f"<tr><th>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>")
        quality = html.escape(json.dumps(metadata["quality_analysis"], indent=2, ensure_ascii=True))
        warnings = html.escape(json.dumps(metadata["warnings"], indent=2, ensure_ascii=True))
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>ShortsMaster Validation Report</title>"
            "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
            "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}"
            "th{width:260px;background:#f4f4f4}pre{background:#111;color:#eee;padding:16px;overflow:auto}</style>"
            "</head><body><h1>ShortsMaster Validation Report</h1>"
            f"<table>{''.join(rows)}</table>"
            "<h2>Quality Analysis</h2><pre>" + quality + "</pre>"
            "<h2>Warnings</h2><pre>" + warnings + "</pre>"
            "</body></html>"
        )
