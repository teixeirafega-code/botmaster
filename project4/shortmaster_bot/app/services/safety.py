from __future__ import annotations

import html
import json
import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.models import ContentScript, ResearchBrief, TrendTopic
from app.services.config import resolve_path, resolve_storage_path
from app.services.database import ShortsMasterDatabase
from app.services.language import LanguageGuard
from app.services.narrative_style import analyze_reddit_narrative_style
from app.services.reddit_story import is_reddit_story_topic
from app.utils.text import clean_text


LOGGER = logging.getLogger(__name__)


class SafetyGuard:
    def __init__(self, db: ShortsMasterDatabase, config: dict):
        self.db = db
        self.config = config
        self.safety_config = config.get("safety", {})
        self.publishing_config = config.get("publishing", {})
        self.language = LanguageGuard(config)

    def evaluate_content(
        self,
        queue_id: int,
        topic: TrendTopic,
        script: ContentScript,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        warnings: list[str] = []
        score = 96.0
        score_reasons = ["baseline safety score starts below 100 until all risk checks have strong evidence"]

        language_decision = self.evaluate_language(script)
        if not language_decision["allowed"]:
            reasons.extend(language_decision["reasons"])
            warnings.extend(language_decision["warnings"])
            score -= 50
            score_reasons.append("major penalty: pt-BR language gate failed")
        else:
            warnings.extend(language_decision["warnings"])
            score_reasons.append("passed mandatory pt-BR language gate")

        spam_reasons = self._spam_reasons(script)
        if spam_reasons:
            reasons.extend(spam_reasons)
            score -= 45
            score_reasons.append("major penalty: spam language detected")
        else:
            score_reasons.append("passed spam language check")

        copyright_reasons = self._copyright_reasons(script)
        if copyright_reasons:
            reasons.extend(copyright_reasons)
            score -= 35
            score_reasons.append("major penalty: copyright risk language detected")
        else:
            score_reasons.append("passed copyright risk phrase check")

        narrative_style = (
            analyze_reddit_narrative_style(script)
            if is_reddit_story_topic(topic)
            else {
                "narrative_naturalness_score": 100.0,
                "disclaimer_leakage_score": 0.0,
                "findings": [],
            }
        )
        story_reasons, story_warnings = self._story_mode_reasons(topic, script)
        if story_reasons:
            reasons.extend(story_reasons)
            score -= 45
            score_reasons.append("major penalty: Reddit story safety policy failed")
        elif is_reddit_story_topic(topic):
            score_reasons.append("passed Reddit story originality and natural narration checks")
        warnings.extend(story_warnings)

        generic_reasons = self._generic_script_reasons(script)
        if generic_reasons:
            reasons.extend(generic_reasons)
            score -= 30
            score_reasons.append("major penalty: generic fallback script language detected")
        else:
            score_reasons.append("passed generic fallback language check")

        word_count = len(script.narration.split())
        min_words = int(self.safety_config.get("min_script_words", 105))
        max_words = int(self.safety_config.get("max_script_words", 190))
        if word_count < min_words:
            warnings.append(f"script has only {word_count} words")
            score -= 20
            score_reasons.append("penalty: script is short for a 60 second Short")
        elif word_count > max_words:
            warnings.append(f"script has {word_count} words")
            score -= 12
            score_reasons.append("penalty: script is long for a 60 second Short")
        else:
            score_reasons.append("passed narration length check")

        if len(script.scenes) < int(self.safety_config.get("min_scene_count", 4)):
            warnings.append("script has too few scenes")
            score -= 15
            score_reasons.append("penalty: too few visual scenes")
        else:
            score_reasons.append("passed scene count check")

        retention = self._retention_quality(topic, script)
        min_hook = float(self.safety_config.get("min_hook_score", 70))
        min_retention = float(self.safety_config.get("min_retention_score", 72))
        min_visual = float(self.safety_config.get("min_visual_interest_score", 70))
        if retention["hook_score"] < min_hook:
            reasons.append(f"hook_score {retention['hook_score']:.1f} is below minimum {min_hook:.1f}")
            score -= 24
            score_reasons.append("major penalty: weak opening hook")
        else:
            score_reasons.append("passed hook retention gate")
        if retention["retention_score"] < min_retention:
            reasons.append(f"retention_score {retention['retention_score']:.1f} is below minimum {min_retention:.1f}")
            score -= 20
            score_reasons.append("major penalty: weak Shorts retention structure")
        else:
            score_reasons.append("passed retention structure gate")
        if retention["visual_interest_score"] < min_visual:
            reasons.append(f"visual_interest_score {retention['visual_interest_score']:.1f} is below minimum {min_visual:.1f}")
            score -= 24
            score_reasons.append("major penalty: low visual interest or static-slide structure")
        else:
            score_reasons.append("passed visual interest gate")
        warnings.extend(retention["warnings"])

        duplicate = self._local_script_duplicate(queue_id, script)
        if duplicate:
            reasons.append(duplicate)
            score -= 50
            score_reasons.append("major penalty: script too similar to previous generated item")
        else:
            score_reasons.append("passed local duplicate script check")

        score = max(0.0, min(100.0, score))
        min_score = float(self.safety_config.get("min_quality_score", 70))
        if score < min_score:
            reasons.append(f"quality score {score:.1f} is below minimum {min_score:.1f}")

        decision = {
            "allowed": not reasons,
            "quality_score": round(score, 1),
            "safety_score": round(score, 1),
            "score_reasons": score_reasons,
            "reasons": reasons,
            "warnings": warnings,
            "checks": {
                "word_count": word_count,
                "scene_count": len(script.scenes),
                "min_quality_score": min_score,
                "topic_key": topic.key,
                "hook_score": retention["hook_score"],
                "curiosity_score": retention["curiosity_score"],
                "storytelling_score": retention["storytelling_score"],
                "retention_score": retention["retention_score"],
                "visual_interest_score": retention["visual_interest_score"],
                "narrative_naturalness_score": narrative_style["narrative_naturalness_score"],
                "disclaimer_leakage_score": narrative_style["disclaimer_leakage_score"],
                "retention_findings": retention["findings"],
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
        LOGGER.info("Safety content decision for queue #%s: %s", queue_id, decision)
        return decision

    def evaluate_language(self, script: ContentScript) -> dict[str, Any]:
        return self.language.evaluate_script(script)

    def evaluate_upload(
        self,
        real_upload_enabled: bool,
        queue_id: int | None = None,
        topic: TrendTopic | None = None,
        script: ContentScript | None = None,
        video_path: Path | None = None,
        queue_item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checklist = self.pre_upload_checklist(
            real_upload_enabled=real_upload_enabled,
            queue_id=queue_id,
            topic=topic,
            script=script,
            video_path=video_path,
            queue_item=queue_item,
        )
        self.write_pre_upload_checklist(checklist)
        if not real_upload_enabled:
            return {
                "allowed": True,
                "reasons": [],
                "warnings": ["real upload disabled; publishing will be simulated"],
                "checklist": checklist,
            }
        return {
            "allowed": checklist["allowed"],
            "reasons": checklist["blockers"],
            "warnings": checklist["warnings"],
            "checklist": checklist,
        }

    def pre_upload_checklist(
        self,
        real_upload_enabled: bool,
        queue_id: int | None,
        topic: TrendTopic | None,
        script: ContentScript | None,
        video_path: Path | None,
        queue_item: dict[str, Any] | None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        warnings: list[str] = []
        checks: list[dict[str, Any]] = []

        paper_mode = bool(self.config.get("app", {}).get("paper_mode", True))
        enable_real_upload = bool(self.publishing_config.get("enable_real_upload", False))
        live_upload_enabled = bool(self.publishing_config.get("live_upload_enabled", False))

        self._add_check(checks, "paper_mode_disabled", not paper_mode, f"PAPER_MODE={paper_mode}")
        self._add_check(checks, "enable_real_upload_true", enable_real_upload, f"ENABLE_REAL_UPLOAD={enable_real_upload}")
        self._add_check(checks, "live_upload_enabled_true", live_upload_enabled, f"LIVE_UPLOAD_ENABLED={live_upload_enabled}")
        self._add_check(checks, "effective_real_upload_enabled", real_upload_enabled, f"publisher.real_upload_enabled={real_upload_enabled}")

        channel_id = clean_text(str(self.publishing_config.get("channel_id", "")))
        self._add_check(checks, "channel_id_configured", bool(channel_id), "channel_id configured" if channel_id else "channel_id missing")

        daily_limit = self.daily_upload_limit()
        uploads_today = self.db.count_real_uploads_today()
        daily_ok = uploads_today < daily_limit
        self._add_check(checks, "daily_upload_limit_available", daily_ok, f"{uploads_today}/{daily_limit} real uploads today")

        quota_limit = int(self.publishing_config.get("youtube_daily_quota_units", 10000))
        upload_units = int(self.publishing_config.get("youtube_upload_quota_units", 1600))
        quota_used = self.db.youtube_quota_used_today()
        quota_ok = quota_used + upload_units <= quota_limit
        self._add_check(checks, "youtube_quota_available", quota_ok, f"{quota_used}+{upload_units}/{quota_limit} units")

        credentials = self._credentials_status()
        self._add_check(checks, "client_secrets_present", credentials["client_secrets_present"], credentials["client_secrets_detail"])
        self._add_check(checks, "oauth_token_present", credentials["token_present"], credentials["token_detail"])

        live_approved = bool(queue_item and int(queue_item.get("approved_for_live_upload") or 0) == 1)
        self._add_check(checks, "approved_for_live_upload", live_approved, "approved_for_live_upload=true" if live_approved else "approved_for_live_upload is not true")

        story_background_mode = str(
            self.config.get("story_mode", {}).get("background_video", {}).get("mode", "")
        ).lower()
        custom_story_background = bool(
            topic
            and is_reddit_story_topic(topic)
            and story_background_mode in {"custom_background_library", "background_library"}
        )
        background_filename = str(queue_item.get("background_filename", "") or "") if queue_item else ""
        background_rights_verified = bool(
            queue_item
            and int(queue_item.get("background_commercial_rights_verified") or 0) == 1
        )
        rights_required = custom_story_background or bool(background_filename)
        self._add_check(
            checks,
            "background_commercial_rights_verified",
            (not rights_required) or background_rights_verified,
            (
                f"background={background_filename or '<missing>'}; commercial_rights_verified="
                f"{background_rights_verified}"
            ),
        )

        quality_score = self._queue_quality_score(queue_item)
        min_upload_quality = float(self.publishing_config.get("min_upload_quality_score", 75))
        self._add_check(
            checks,
            "quality_score_minimum",
            quality_score >= min_upload_quality,
            f"quality_score={quality_score:.1f}; minimum={min_upload_quality:.1f}",
        )

        safety_score = self._queue_safety_score(queue_item)
        min_upload_safety = float(self.publishing_config.get("min_upload_safety_score", 90))
        self._add_check(
            checks,
            "safety_score_minimum",
            safety_score >= min_upload_safety,
            f"safety_score={safety_score:.1f}; minimum={min_upload_safety:.1f}",
        )

        language_script = script or self._queue_script(queue_item)
        if language_script:
            language_decision = self.evaluate_language(language_script)
            required_language = str(language_decision["required_language"])
            self._add_check(
                checks,
                "script_language_pt_br",
                str(language_decision["script_language"]).lower() == required_language.lower(),
                f"script_language={language_decision['script_language']}; required={required_language}",
            )
            self._add_check(
                checks,
                "narration_language_pt_br",
                str(language_decision["narration_language"]).lower() == required_language.lower(),
                f"narration_language={language_decision['narration_language']}; required={required_language}",
            )
            self._add_check(
                checks,
                "subtitle_language_pt_br",
                str(language_decision["subtitle_language"]).lower() == required_language.lower(),
                f"subtitle_language={language_decision['subtitle_language']}; required={required_language}",
            )
            self._add_check(
                checks,
                "title_language_pt_br",
                str(language_decision["title_language"]).lower() == required_language.lower(),
                f"title_language={language_decision['title_language']}; required={required_language}",
            )
            self._add_check(
                checks,
                "english_output_absent",
                not language_decision["english_residue"],
                json.dumps(language_decision["english_residue"], ensure_ascii=True),
            )
        else:
            self._add_check(checks, "language_validation_available", False, "script missing; cannot validate pt-BR output")

        research_status = self._research_status(queue_item)
        trust_score = float(research_status.get("trust_score", 0.0))
        trust_threshold = float(research_status.get("trust_threshold", self.config.get("research", {}).get("trust_min_score", 70)))
        self._add_check(
            checks,
            "trust_score_minimum",
            research_status["exists"] and trust_score >= trust_threshold,
            f"trust_score={trust_score:.1f}; threshold={trust_threshold:.1f}; research_exists={research_status['exists']}",
        )
        freshness_score = float(research_status.get("freshness_score", 0.0))
        freshness_threshold = float(research_status.get("freshness_threshold", self.config.get("research", {}).get("freshness_min_score", 70)))
        freshness_required = bool(research_status.get("requires_fresh_source", False))
        freshness_ok = research_status["exists"] and ((not freshness_required) or freshness_score >= freshness_threshold)
        self._add_check(
            checks,
            "freshness_score_current_topic",
            freshness_ok,
            f"freshness_score={freshness_score:.1f}; threshold={freshness_threshold:.1f}; required={freshness_required}",
        )

        topic_key = topic.key if topic else str(queue_item.get("topic_key", "")) if queue_item else ""
        duplicate = self.db.has_duplicate_real_upload(
            int(queue_id or 0),
            topic_key,
            str(video_path) if video_path else None,
            script.title if script else str(queue_item.get("title", "")) if queue_item else "",
        )
        duplicate_ok = not duplicate["topic_duplicate"] and not duplicate["video_duplicate"]
        self._add_check(checks, "duplicate_upload_absent", duplicate_ok, json.dumps(duplicate, ensure_ascii=True))
        duplicate_title_ok = not duplicate.get("title_duplicate")
        self._add_check(checks, "duplicate_title_absent", duplicate_title_ok, json.dumps(duplicate.get("title_duplicate"), ensure_ascii=True))

        video_exists = bool(video_path and video_path.exists())
        self._add_check(checks, "video_file_exists", video_exists, str(video_path) if video_path else "video path missing")

        performance_decision = self._performance_decision()
        self._add_check(
            checks,
            "performance_guard_passed",
            bool(performance_decision["allowed"]),
            "; ".join(performance_decision["reasons"] or performance_decision["warnings"] or ["performance guard passed"]),
        )

        live_blockers: list[str] = []
        for check in checks:
            if not check["passed"]:
                live_blockers.append(f"{check['name']}: {check['detail']}")
        reasons = list(live_blockers)
        warnings.extend(performance_decision["warnings"])

        if not real_upload_enabled:
            warnings.append("checklist generated in simulation mode; real upload gates are not active")
            reasons = []

        return {
            "allowed": not reasons,
            "simulation_allowed": not real_upload_enabled,
            "real_upload_allowed": not live_blockers,
            "blockers": reasons,
            "real_upload_blockers": live_blockers,
            "warnings": warnings,
            "checks": checks,
            "queue_id": queue_id,
            "topic": topic.title if topic else queue_item.get("title") if queue_item else None,
            "video_path": str(video_path) if video_path else None,
            "real_upload_enabled": real_upload_enabled,
            "paper_mode": paper_mode,
            "enable_real_upload": enable_real_upload,
            "live_upload_enabled": live_upload_enabled,
            "channel_id": channel_id,
            "daily_upload_limit": daily_limit,
            "uploads_today": uploads_today,
            "youtube_quota_used_today": quota_used,
            "youtube_upload_quota_units": upload_units,
            "youtube_daily_quota_units": quota_limit,
            "credentials": credentials,
            "duplicate": duplicate,
            "quality_score": quality_score,
            "min_upload_quality_score": min_upload_quality,
            "safety_score": safety_score,
            "min_upload_safety_score": min_upload_safety,
            "research": research_status,
        }

    def write_pre_upload_checklist(self, checklist: dict[str, Any]) -> dict[str, str]:
        reports_dir = resolve_storage_path(self.config, self.config.get("storage", {}).get("reports_dir", "reports"))
        reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = reports_dir / "youtube_pre_upload_checklist.json"
        html_path = reports_dir / "youtube_pre_upload_checklist.html"
        payload = {
            "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat(),
            **checklist,
        }
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        html_path.write_text(self._pre_upload_checklist_html(payload), encoding="utf-8")
        return {"json": str(json_path), "html": str(html_path)}

    def _add_check(self, checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    def _credentials_status(self) -> dict[str, Any]:
        secrets = resolve_path(self.config, self.publishing_config.get("youtube_client_secrets_file", ""))
        token = resolve_path(self.config, self.publishing_config.get("youtube_token_file", ""))
        client_env_name = str(self.publishing_config.get("youtube_client_secret_json_env", "YOUTUBE_CLIENT_SECRET_JSON"))
        token_env_name = str(self.publishing_config.get("youtube_token_json_env", "YOUTUBE_TOKEN_JSON"))
        client_env_present = bool(os.getenv(client_env_name))
        token_env_present = bool(os.getenv(token_env_name))
        client_present = client_env_present or secrets.exists()
        token_present = token_env_present or token.exists()
        return {
            "client_secrets_file_path": str(secrets),
            "client_secrets_file_exists": secrets.exists(),
            "client_secret_env_var": client_env_name,
            "client_secret_env_present": client_env_present,
            "client_secrets_present": client_present,
            "client_secrets_detail": f"env:{client_env_name}" if client_env_present else str(secrets),
            "token_file_path": str(token),
            "token_file_exists": token.exists(),
            "token_env_var": token_env_name,
            "token_env_present": token_env_present,
            "token_present": token_present,
            "token_detail": f"env:{token_env_name}" if token_env_present else str(token),
        }

    def credentials_status(self) -> dict[str, Any]:
        return self._credentials_status()

    def _queue_quality_score(self, queue_item: dict[str, Any] | None) -> float:
        if not queue_item:
            return 0.0
        try:
            return float(queue_item.get("quality_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _queue_safety_score(self, queue_item: dict[str, Any] | None) -> float:
        if not queue_item or not queue_item.get("safety_json"):
            return 0.0
        try:
            payload = json.loads(str(queue_item["safety_json"]))
        except json.JSONDecodeError:
            return 0.0
        candidates = [
            payload.get("safety_score"),
            payload.get("quality_score"),
            payload.get("content", {}).get("safety_score") if isinstance(payload.get("content"), dict) else None,
            payload.get("safety", {}).get("safety_score") if isinstance(payload.get("safety"), dict) else None,
        ]
        for candidate in candidates:
            try:
                if candidate is not None:
                    return float(candidate)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _queue_script(self, queue_item: dict[str, Any] | None) -> ContentScript | None:
        if not queue_item or not queue_item.get("script_json"):
            return None
        try:
            return ContentScript.from_dict(json.loads(str(queue_item["script_json"])))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _research_status(self, queue_item: dict[str, Any] | None) -> dict[str, Any]:
        if not queue_item or not queue_item.get("research_json"):
            return {
                "exists": False,
                "trust_score": 0.0,
                "trust_threshold": float(self.config.get("research", {}).get("trust_min_score", 70)),
                "freshness_score": 0.0,
                "freshness_threshold": float(self.config.get("research", {}).get("freshness_min_score", 70)),
                "requires_fresh_source": False,
            }
        try:
            research = ResearchBrief.from_dict(json.loads(str(queue_item["research_json"])))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return {
                "exists": False,
                "trust_score": 0.0,
                "trust_threshold": float(self.config.get("research", {}).get("trust_min_score", 70)),
                "freshness_score": 0.0,
                "freshness_threshold": float(self.config.get("research", {}).get("freshness_min_score", 70)),
                "requires_fresh_source": False,
            }
        return {
            "exists": True,
            "trust_score": float(research.trust_score),
            "trust_threshold": float(research.trust_threshold),
            "trust_required": bool(research.requires_trusted_source),
            "freshness_score": float(research.freshness_score),
            "freshness_threshold": float(research.freshness_threshold),
            "requires_fresh_source": bool(research.requires_fresh_source),
            "has_fresh_source": research.has_fresh_source,
            "has_trusted_source": research.has_trusted_source,
        }

    def _pre_upload_checklist_html(self, checklist: dict[str, Any]) -> str:
        rows = []
        for check in checklist.get("checks", []):
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(check['name']))}</td>"
                f"<td>{'PASS' if check['passed'] else 'BLOCK'}</td>"
                f"<td>{html.escape(str(check['detail']))}</td>"
                "</tr>"
            )
        body = html.escape(json.dumps(checklist, indent=2, ensure_ascii=True, default=str))
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>YouTube Pre-upload Checklist</title>"
            "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left}"
            "th{background:#f4f4f4}pre{background:#111;color:#eee;padding:16px;overflow:auto}</style>"
            "</head><body><h1>YouTube Pre-upload Checklist</h1>"
            f"<p><strong>Current operation allowed:</strong> {checklist.get('allowed')}</p>"
            f"<p><strong>Real upload allowed:</strong> {checklist.get('real_upload_allowed')}</p>"
            "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table><h2>Raw JSON</h2><pre>{body}</pre></body></html>"
        )

    def daily_upload_limit(self) -> int:
        requested = int(self.publishing_config.get("daily_upload_limit", 1))
        hard_max = int(self.publishing_config.get("max_daily_upload_limit", 5))
        return max(0, min(requested, hard_max))

    def _spam_reasons(self, script: ContentScript) -> list[str]:
        text = f"{script.title} {script.hook} {script.narration}"
        compact = clean_text(text)
        lower = compact.lower()
        reasons: list[str] = []
        spam_terms = self.safety_config.get(
            "spam_terms",
            [
                "click here",
                "free followers",
                "guaranteed income",
                "get rich quick",
                "make money fast",
                "miracle cure",
                "shocking secret",
                "you won't believe",
                "100% guaranteed",
            ],
        )
        for term in spam_terms:
            if str(term).lower() in lower:
                reasons.append(f"spam phrase detected: {term}")
        if compact.count("!") >= 4:
            reasons.append("excessive exclamation marks")
        title_letters = [char for char in script.title if char.isalpha()]
        if len(title_letters) >= 12:
            uppercase_ratio = sum(char.isupper() for char in title_letters) / len(title_letters)
            if uppercase_ratio > 0.7:
                reasons.append("title uses excessive capitalization")
        return reasons

    def _copyright_reasons(self, script: ContentScript) -> list[str]:
        joined_prompts = " ".join(scene.get("image_prompt", "") for scene in script.scenes)
        lower = f"{script.title} {script.narration} {joined_prompts}".lower()
        prohibited = self.safety_config.get(
            "copyright_terms",
            [
                "copyrighted clip",
                "official clip",
                "movie footage",
                "music video",
                "song lyrics",
                "use the lyrics",
                "trailer footage",
                "ripped from",
            ],
        )
        return [f"copyright risk phrase detected: {term}" for term in prohibited if term in lower]

    def _generic_script_reasons(self, script: ContentScript) -> list[str]:
        text = f"{script.title} {script.hook} {script.description} {script.narration}".lower()
        generic_terms = [
            "why it is trending",
            "gaining attention",
            "people are talking about",
            "here is what you need to know",
            "this is important",
            "suddenly everywhere",
            "signal worth watching",
            "por que está em alta",
            "ganhando atenção",
            "todo mundo está falando",
            "isso é importante",
        ]
        hits = [term for term in generic_terms if term in text]
        reasons: list[str] = []
        if re.search(r":\s*why it is trending\b", script.title.lower()):
            reasons.append("generic fallback title detected")
        if len(hits) >= 3:
            reasons.append("generic fallback script phrases detected: " + ", ".join(hits[:5]))
        return reasons

    def _retention_quality(self, topic: TrendTopic, script: ContentScript) -> dict[str, Any]:
        hook_score, hook_findings = self._hook_score(topic, script.hook)
        curiosity_score, curiosity_findings = self._curiosity_score(script)
        storytelling_score, storytelling_findings = self._storytelling_score(script)
        retention_score, retention_findings = self._retention_score(script, hook_score)
        visual_score, visual_findings = self._visual_interest_score(topic, script)
        findings = [
            *hook_findings,
            *curiosity_findings,
            *storytelling_findings,
            *retention_findings,
            *visual_findings,
        ]
        return {
            "hook_score": round(hook_score, 1),
            "curiosity_score": round(curiosity_score, 1),
            "storytelling_score": round(storytelling_score, 1),
            "retention_score": round(retention_score, 1),
            "visual_interest_score": round(visual_score, 1),
            "findings": findings,
            "warnings": findings,
        }

    def _hook_score(self, topic: TrendTopic, hook: str) -> tuple[float, list[str]]:
        hook = clean_text(hook)
        lower = hook.lower()
        words = hook.split()
        findings: list[str] = []
        if is_reddit_story_topic(topic) and "reddit" in lower and "?" in hook:
            score = 94.0
            if len(words) > 18:
                score -= min(24, (len(words) - 18) * 3)
                findings.append("Reddit prompt is too long for the opening")
            return max(0.0, min(96.0, score)), findings
        score = 82.0
        if len(words) > 10:
            score -= min(28, (len(words) - 10) * 4)
            findings.append("hook should land inside the first 2 seconds")
        if len(words) < 4:
            score -= 10
            findings.append("hook is too thin to create curiosity")
        generic = [
            "suddenly everywhere",
            "gaining attention",
            "people are talking",
            "why it is trending",
            "todo mundo está falando",
            "ganhando atenção",
            "por que está em alta",
        ]
        if any(phrase in lower for phrase in generic):
            score -= 30
            findings.append("hook uses generic trend-report language")
        curiosity_terms = [
            "clue", "hidden", "miss", "twist", "trap", "why", "how", "before", "after", "changes",
            "pista", "escond", "perd", "virada", "armadilha", "por que", "como", "antes", "depois", "muda",
        ]
        if not any(term in lower for term in curiosity_terms):
            score -= 18
            findings.append("hook has no curiosity gap")
        if not (re.search(r"\b\d{1,4}\b", hook) or self._topic_overlap(topic.title, hook)):
            score -= 12
            findings.append("hook lacks a concrete topic anchor")
        return max(0.0, min(96.0, score)), findings

    def _retention_score(self, script: ContentScript, hook_score: float) -> tuple[float, list[str]]:
        findings: list[str] = []
        sentences = [sentence for sentence in re.split(r"(?<=[.!?])\s+", clean_text(script.narration)) if sentence]
        score = 78.0
        if hook_score >= 80:
            score += 6
        else:
            score -= 12
            findings.append("weak hook lowers retention")
        if sentences:
            avg_words = sum(len(sentence.split()) for sentence in sentences) / len(sentences)
            if avg_words <= 16:
                score += 6
            else:
                score -= min(20, (avg_words - 16) * 2)
                findings.append(f"average sentence length is report-like ({avg_words:.1f} words)")
        if len(script.scenes) >= int(self.safety_config.get("min_scene_count", 8)):
            score += 5
        else:
            score -= 14
            findings.append("too few scenes for Shorts pacing")
        lower = script.narration.lower()
        required_beats = {
            "curiosity": ["clue", "hidden", "odd", "strange", "trap", "miss", "pista", "escond", "estranho", "armadilha", "quando", "até que"],
            "escalation": ["then", "first", "next", "worse", "primeiro", "depois", "em seguida", "quando", "de volta", "então", "mas"],
            "reveal": ["reveal", "payoff", "result", "revelação", "descobri", "acharam", "segurava", "não havia", "havia alguém"],
            "fast_ending": ["remember", "watch this", "lembre", "só parei", "pedi demissão", "até hoje", "ali embaixo", "não existia", "cinquenta anos", "pneus"],
        }
        for label, terms in required_beats.items():
            if not any(term in lower for term in terms):
                score -= 10
                findings.append(f"missing {label} beat")
        academic_terms = ["therefore", "moreover", "furthermore", "in conclusion", "significant", "utilize"]
        if any(term in lower for term in academic_terms):
            score -= 14
            findings.append("academic wording hurts Shorts retention")
        return max(0.0, min(96.0, score)), findings

    def _curiosity_score(self, script: ContentScript) -> tuple[float, list[str]]:
        text = f"{script.hook} {script.narration}".lower()
        findings: list[str] = []
        score = 76.0
        curiosity_terms = [
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
            "what changed",
            "pista",
            "escond",
            "estranho",
            "errado",
            "faltando",
            "antes",
            "depois",
            "virada",
            "armadilha",
            "o que mudou",
            "quando",
            "instinto",
            "congelar",
            "navalha",
            "sumiu",
            "desapareceu",
        ]
        hits = sum(1 for term in curiosity_terms if term in text)
        score += min(18, hits * 4)
        if hits < 2:
            score -= 20
            findings.append("story has too little curiosity language")
        if "then it gets sharper" in text or "next" in text or "depois fica" in text or "em seguida" in text:
            score += 4
        if any(phrase in text for phrase in ["here is what you need to know", "gaining attention", "ganhando atenção"]):
            score -= 18
            findings.append("curiosity is diluted by generic explainer phrasing")
        return max(0.0, min(96.0, score)), findings

    def _storytelling_score(self, script: ContentScript) -> tuple[float, list[str]]:
        style = analyze_reddit_narrative_style(script)
        if style["reddit_prompt_present"] and (
            style["first_person_signal_count"] >= 3 or style["close_third_person_present"]
        ):
            score = 92.0
            findings = list(style["findings"])
            if style["average_sentence_words"] > 15:
                score -= min(18, (style["average_sentence_words"] - 15) * 2)
            return max(0.0, min(96.0, score)), findings
        text = script.narration.lower()
        findings: list[str] = []
        score = 74.0
        beats = {
            "hook": [script.hook.lower()],
            "curiosity": ["curiosity", "clue", "hidden", "strange", "odd", "curiosidade", "pista", "escond", "estranho"],
            "escalation": ["first", "then", "next", "gets sharper", "worse", "stacking", "primeiro", "depois", "em seguida", "pior", "tensão"],
            "reveal": ["reveal", "payoff", "changed", "turns", "revelação", "recompensa", "mudou", "virada"],
            "ending": ["fast ending", "remember", "ending", "final rápido", "lembre", "final"],
        }
        present = 0
        for label, terms in beats.items():
            if any(term and term in text for term in terms):
                present += 1
            else:
                findings.append(f"missing story {label} beat")
        score += present * 5
        sentences = [sentence for sentence in re.split(r"(?<=[.!?])\s+", clean_text(script.narration)) if sentence]
        if sentences:
            avg_words = sum(len(sentence.split()) for sentence in sentences) / len(sentences)
            if avg_words <= 15:
                score += 5
            else:
                score -= min(16, (avg_words - 15) * 2)
                findings.append(f"story sentences are too long on average ({avg_words:.1f} words)")
        if len(script.scenes) >= int(self.safety_config.get("min_scene_count", 8)):
            score += 4
        return max(0.0, min(96.0, score)), findings

    def _visual_interest_score(self, topic: TrendTopic, script: ContentScript) -> tuple[float, list[str]]:
        findings: list[str] = []
        score = 78.0
        min_scenes = int(self.safety_config.get("min_scene_count", 8))
        story_background_mode = str(
            self.config.get("story_mode", {}).get("background_video", {}).get("mode", "")
        ).lower()
        if is_reddit_story_topic(topic) and story_background_mode in {
            "retention_categories",
            "custom_background_library",
            "background_library",
        }:
            if len(script.scenes) >= min_scenes:
                score = 90.0 + min(6, (len(script.scenes) - min_scenes + 1) * 2)
            else:
                score = 62.0
                findings.append(f"only {len(script.scenes)} subtitle beats; minimum visual variety is {min_scenes}")
            return max(0.0, min(96.0, score)), findings
        if len(script.scenes) >= min_scenes:
            score += min(10, (len(script.scenes) - min_scenes + 1) * 2)
        else:
            score -= 24
            findings.append(f"only {len(script.scenes)} scenes; minimum visual variety is {min_scenes}")
        topic_terms = self._token_set(topic.title)
        generic_prompt_terms = {
            "abstract",
            "gradient",
            "background",
            "text",
            "title",
            "slide",
            "data",
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
        generic_scenes = 0
        relevant_scenes = 0
        long_captions = 0
        for scene in script.scenes:
            caption = clean_text(scene.get("caption", ""))
            prompt = clean_text(scene.get("image_prompt", ""))
            prompt_terms = self._token_set(prompt)
            if len(caption.split()) > 6 or len(caption) > 42:
                long_captions += 1
            if topic_terms and topic_terms & prompt_terms:
                relevant_scenes += 1
            if len(generic_prompt_terms & prompt_terms) >= 3 or "text-only" in prompt.lower() or "static gradient" in prompt.lower():
                generic_scenes += 1
        if long_captions:
            score -= min(18, long_captions * 4)
            findings.append("on-screen captions are too long")
        if script.scenes and relevant_scenes / len(script.scenes) < 0.65:
            score -= 18
            findings.append("image prompts are not tied closely enough to the topic")
        if generic_scenes:
            score -= min(28, generic_scenes * 7)
            findings.append("image prompts rely on generic or text-slide visuals")
        return max(0.0, min(96.0, score)), findings

    def _story_mode_reasons(self, topic: TrendTopic, script: ContentScript) -> tuple[list[str], list[str]]:
        if not is_reddit_story_topic(topic):
            return [], []
        reasons: list[str] = []
        warnings: list[str] = []
        text = clean_text(f"{script.title} {script.hook} {script.description} {script.narration}")
        lower = text.lower()
        style = analyze_reddit_narrative_style(script)
        min_naturalness = float(self.safety_config.get("min_narrative_naturalness_score", 75))
        if style["disclaimer_leakage_score"] > 0:
            reasons.append(
                "public Reddit story script contains source disclaimer language: "
                + ", ".join(style["disclaimer_hits"][:5])
            )
        if style["narrative_naturalness_score"] < min_naturalness:
            reasons.append(
                f"narrative_naturalness_score {style['narrative_naturalness_score']:.1f} "
                f"is below minimum {min_naturalness:.1f}"
            )
        warnings.extend(style["findings"])
        fact_claims = ["this really happened", "confirmed true", "proves that", "the truth is", "isso realmente aconteceu", "confirmado como verdade", "a verdade é"]
        if any(term in lower for term in fact_claims):
            reasons.append("Reddit story script presents an anecdote as confirmed fact")

        source_text = clean_text(str((topic.raw or {}).get("source_text_for_similarity", "")))
        overlap = self._verbatim_overlap(source_text, text)
        if overlap:
            reasons.append(f"Reddit source text copied too closely: '{overlap}'")
        story_source = (topic.raw or {}).get("story_source") or {}
        if isinstance(story_source, dict) and story_source.get("source_kind") == "top_comment_story":
            warnings.append("story source came from a Reddit comment; verbatim comment copying check applied")
        return reasons, warnings

    def _verbatim_overlap(self, source_text: str, generated_text: str) -> str | None:
        source_words = self._normalized_words(source_text)
        generated_words = self._normalized_words(generated_text)
        ngram_size = int(self.safety_config.get("source_copy_ngram_words", 8))
        if len(source_words) < ngram_size or len(generated_words) < ngram_size:
            return None
        source_ngrams = {
            tuple(source_words[index : index + ngram_size])
            for index in range(len(source_words) - ngram_size + 1)
        }
        for index in range(len(generated_words) - ngram_size + 1):
            ngram = tuple(generated_words[index : index + ngram_size])
            if ngram in source_ngrams:
                return " ".join(ngram)
        return None

    def _normalized_words(self, text: str) -> list[str]:
        return [
            word
            for word in re.findall(r"[a-z0-9à-öø-ÿ']+", clean_text(text).lower())
            if word not in {"the", "and", "that", "this", "with", "from", "que", "para", "com", "uma", "um"}
        ]

    def _topic_overlap(self, topic: str, text: str) -> bool:
        topic_terms = self._token_set(topic)
        return bool(topic_terms and topic_terms & self._token_set(text))

    def _token_set(self, text: str) -> set[str]:
        stopwords = {
            "the", "and", "for", "with", "from", "that", "this", "what", "why", "how",
            "are", "was", "were", "into", "about", "trend", "trending", "shorts",
            "que", "para", "com", "uma", "por", "como", "isso", "este", "esta",
        }
        return {
            word
            for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", text.lower())
            if len(word) > 2 and word not in stopwords
        }

    def _local_script_duplicate(self, queue_id: int, script: ContentScript) -> str | None:
        threshold = float(self.safety_config.get("script_similarity_block_threshold", 0.92))
        current = self._similarity_text(script.to_dict())
        for payload in self.db.list_recent_script_payloads(exclude_queue_id=queue_id):
            previous = self._similarity_text(payload["script"])
            if not previous:
                continue
            similarity = SequenceMatcher(None, current, previous).ratio()
            if similarity >= threshold:
                return f"script is too similar to queue item #{payload['id']} ({similarity:.2f})"
        return None

    def _similarity_text(self, script_payload: dict[str, Any]) -> str:
        text = " ".join(
            [
                str(script_payload.get("title", "")),
                str(script_payload.get("hook", "")),
                str(script_payload.get("narration", "")),
            ]
        )
        text = clean_text(text).lower()
        return re.sub(r"[^a-z0-9 ]+", "", text)

    def _performance_decision(self) -> dict[str, Any]:
        guard = self.safety_config.get("performance_guard", {})
        if not guard.get("enabled", True):
            return {"allowed": True, "reasons": [], "warnings": []}

        min_recent = int(guard.get("min_recent_videos", 3))
        recent = self.db.recent_real_video_metrics(limit=int(guard.get("recent_video_window", 5)))
        if len(recent) < min_recent:
            return {
                "allowed": True,
                "reasons": [],
                "warnings": [f"performance guard waiting for {min_recent} recent videos"],
            }

        avg_views = sum(int(item["views"]) for item in recent) / len(recent)
        avg_like_rate = sum(
            (int(item["likes"]) / max(1, int(item["views"]))) for item in recent
        ) / len(recent)
        min_avg_views = float(guard.get("min_avg_views", 50))
        min_avg_like_rate = float(guard.get("min_avg_like_rate", 0.01))

        reasons: list[str] = []
        if avg_views < min_avg_views:
            reasons.append(f"recent average views too low ({avg_views:.1f} < {min_avg_views:.1f})")
        if avg_like_rate < min_avg_like_rate:
            reasons.append(
                f"recent average like rate too low ({avg_like_rate:.3f} < {min_avg_like_rate:.3f})"
            )
        return {
            "allowed": not reasons,
            "reasons": reasons,
            "warnings": [],
        }
