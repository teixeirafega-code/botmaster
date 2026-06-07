from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from app.models import ResearchBrief, TrendTopic
from app.utils.retry import retry
from app.utils.text import clean_text, split_sentences


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RedditStoryCandidate:
    subreddit: str
    post_id: str
    title: str
    source_text: str
    source_url: str
    source_kind: str
    ups: int
    comments: int
    upvote_ratio: float
    created_utc: float
    priority_labels: list[str]
    selection_score: float
    rejection_reasons: list[str] = field(default_factory=list)
    author: str = ""
    comment_id: str = ""
    comment_score: int = 0
    source_platform: str = "Reddit"
    fallback_reason: str = ""

    @property
    def created_at_iso(self) -> str:
        return datetime.fromtimestamp(self.created_utc, tz=timezone.utc).replace(microsecond=0).isoformat()

    @property
    def source_hash(self) -> str:
        return hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()


class RedditStoryService:
    source_name = "reddit_story"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.story_config = config.get("story_mode", {})
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.story_config.get(
                    "user_agent",
                    "ShortsMasterBot/1.0 reddit story shorts mode",
                )
            }
        )
        self.last_candidates: list[RedditStoryCandidate] = []
        self.last_rejections: list[dict[str, Any]] = []
        self.last_source_failures: list[dict[str, str]] = []
        self._oauth_token: str = ""
        self._oauth_token_expires_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.story_config.get("enabled", False))

    def find_story(self) -> TrendTopic | None:
        candidates = self.fetch_candidates()
        if not candidates:
            LOGGER.warning("Reddit story mode found no eligible story candidates")
            return None
        candidates.sort(key=lambda item: item.selection_score, reverse=True)
        selected = candidates[0]
        LOGGER.info(
            "Selected Reddit story from r/%s with score %.1f: %s",
            selected.subreddit,
            selected.selection_score,
            selected.title,
        )
        return self.topic_from_candidate(selected)

    def fetch_candidates(self) -> list[RedditStoryCandidate]:
        self.last_candidates = []
        self.last_rejections = []
        self.last_source_failures = []
        for subreddit in self.story_config.get("subreddits", []):
            subreddit = str(subreddit).strip()
            if not subreddit:
                continue
            try:
                posts = self._fetch_subreddit_posts(subreddit)
            except Exception as exc:
                LOGGER.warning("Reddit story source failed for r/%s: %s", subreddit, exc)
                continue
            for post in posts:
                candidate = self._candidate_from_post(post, subreddit)
                if candidate and not candidate.rejection_reasons:
                    self.last_candidates.append(candidate)
        if not self.last_candidates and self._original_fallback_enabled():
            self.last_candidates.extend(self._fallback_candidates())
        return list(self.last_candidates)

    @retry(attempts=3, delay_seconds=1.5, exceptions=(requests.RequestException,))
    def _fetch_subreddit_posts(self, subreddit: str) -> list[dict[str, Any]]:
        sort = str(self.story_config.get("sort", "top")).strip().lower()
        if sort not in {"hot", "top", "new", "rising"}:
            sort = "top"
        limit = int(self.story_config.get("limit_per_subreddit", 25))
        params: dict[str, Any] = {"limit": limit, "raw_json": 1}
        if sort == "top":
            params["t"] = str(self.story_config.get("time_filter", "week"))
        response = self.session.get(
            f"https://www.reddit.com/r/{subreddit}/{sort}.json",
            params=params,
            timeout=float(self.story_config.get("timeout_seconds", 20)),
        )
        if response.status_code == 403:
            LOGGER.warning("Reddit blocked r/%s story JSON with HTTP 403", subreddit)
            self._record_source_failure(subreddit, "public_json", "HTTP 403")
            return self._fetch_subreddit_posts_oauth(subreddit, sort, params)
        if response.status_code == 429:
            raise requests.RequestException("Reddit rate limit reached")
        response.raise_for_status()
        payload = response.json()
        return [child.get("data", {}) for child in payload.get("data", {}).get("children", [])]

    @retry(attempts=2, delay_seconds=1.0, exceptions=(requests.RequestException,))
    def _fetch_subreddit_posts_oauth(
        self,
        subreddit: str,
        sort: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        token = self._reddit_oauth_token()
        if not token:
            LOGGER.warning("Reddit official API fallback skipped for r/%s: credentials unavailable", subreddit)
            self._record_source_failure(subreddit, "official_api", "credentials unavailable")
            return []
        headers = {"Authorization": f"Bearer {token}", "User-Agent": self._user_agent()}
        response = self.session.get(
            f"https://oauth.reddit.com/r/{subreddit}/{sort}",
            params=params,
            headers=headers,
            timeout=float(self.story_config.get("timeout_seconds", 20)),
        )
        if response.status_code in {401, 403}:
            self._record_source_failure(subreddit, "official_api", f"HTTP {response.status_code}")
            LOGGER.warning(
                "Reddit official API fallback failed for r/%s with HTTP %s",
                subreddit,
                response.status_code,
            )
            return []
        if response.status_code == 429:
            raise requests.RequestException("Reddit official API rate limit reached")
        response.raise_for_status()
        payload = response.json()
        return [child.get("data", {}) for child in payload.get("data", {}).get("children", [])]

    def _candidate_from_post(self, post: dict[str, Any], subreddit: str) -> RedditStoryCandidate | None:
        title = clean_text(str(post.get("title", "")))
        post_id = clean_text(str(post.get("id", "")))
        permalink = clean_text(str(post.get("permalink", "")))
        source_url = f"https://www.reddit.com{permalink}" if permalink else clean_text(str(post.get("url", "")))
        ups = int(post.get("ups") or post.get("score") or 0)
        comments = int(post.get("num_comments") or 0)
        upvote_ratio = float(post.get("upvote_ratio") or 0.0)
        created_utc = float(post.get("created_utc") or time.time())
        rejection_reasons = self._post_rejection_reasons(post, title, ups, comments)

        selftext = self._usable_source_text(str(post.get("selftext", "")))
        source_kind = "post_selftext"
        comment_id = ""
        comment_score = 0
        if len(selftext) < int(self.story_config.get("min_story_chars", 450)):
            comment = self._best_comment_story(post_id) if self.story_config.get("use_top_comments", True) else None
            if comment:
                selftext = self._usable_source_text(str(comment.get("body", "")))
                source_kind = "top_comment_story"
                comment_id = clean_text(str(comment.get("id", "")))
                comment_score = int(comment.get("score") or 0)

        source_text = clean_text(f"{title}. {selftext}")
        if len(source_text) < int(self.story_config.get("min_story_chars", 450)):
            rejection_reasons.append("story text below minimum length")
        if len(source_text.split()) < int(self.story_config.get("min_story_words", 90)):
            rejection_reasons.append("story text below minimum word count")

        priority_labels = self._priority_labels(subreddit, title, source_text)
        if not priority_labels:
            rejection_reasons.append("story does not match priority retention categories")

        score = self._selection_score(
            subreddit=subreddit,
            source_text=source_text,
            ups=ups,
            comments=comments,
            upvote_ratio=upvote_ratio,
            created_utc=created_utc,
            priority_labels=priority_labels,
            comment_score=comment_score,
        )
        candidate = RedditStoryCandidate(
            subreddit=subreddit,
            post_id=post_id,
            title=title,
            source_text=source_text,
            source_url=source_url,
            source_kind=source_kind,
            ups=ups,
            comments=comments,
            upvote_ratio=upvote_ratio,
            created_utc=created_utc,
            priority_labels=priority_labels,
            selection_score=round(score, 2),
            rejection_reasons=rejection_reasons,
            author=clean_text(str(post.get("author", ""))),
            comment_id=comment_id,
            comment_score=comment_score,
        )
        if rejection_reasons:
            self.last_rejections.append(self._candidate_rejection_report(candidate))
            return None
        return candidate

    def _post_rejection_reasons(self, post: dict[str, Any], title: str, ups: int, comments: int) -> list[str]:
        reasons: list[str] = []
        if len(title) < 12:
            reasons.append("title too short")
        if post.get("stickied"):
            reasons.append("stickied post")
        if post.get("over_18"):
            reasons.append("over_18 post")
        if post.get("spoiler"):
            reasons.append("spoiler post")
        if clean_text(str(post.get("removed_by_category", ""))):
            reasons.append("removed post")
        if ups < int(self.story_config.get("min_upvotes", 2500)):
            reasons.append("upvotes below threshold")
        if comments < int(self.story_config.get("min_comments", 120)):
            reasons.append("comments below threshold")
        return reasons

    @retry(attempts=2, delay_seconds=1.0, exceptions=(requests.RequestException,))
    def _fetch_comments(self, post_id: str) -> list[dict[str, Any]]:
        if not post_id:
            return []
        response = self.session.get(
            f"https://www.reddit.com/comments/{post_id}.json",
            params={"sort": "top", "limit": int(self.story_config.get("comment_fetch_limit", 8)), "raw_json": 1},
            timeout=float(self.story_config.get("timeout_seconds", 20)),
        )
        if response.status_code == 403:
            return self._fetch_comments_oauth(post_id)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        return [child.get("data", {}) for child in payload[1].get("data", {}).get("children", [])]

    @retry(attempts=2, delay_seconds=1.0, exceptions=(requests.RequestException,))
    def _fetch_comments_oauth(self, post_id: str) -> list[dict[str, Any]]:
        token = self._reddit_oauth_token()
        if not token:
            return []
        response = self.session.get(
            f"https://oauth.reddit.com/comments/{post_id}",
            params={"sort": "top", "limit": int(self.story_config.get("comment_fetch_limit", 8)), "raw_json": 1},
            headers={"Authorization": f"Bearer {token}", "User-Agent": self._user_agent()},
            timeout=float(self.story_config.get("timeout_seconds", 20)),
        )
        if response.status_code in {401, 403, 404}:
            return []
        if response.status_code == 429:
            raise requests.RequestException("Reddit official comments API rate limit reached")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        return [child.get("data", {}) for child in payload[1].get("data", {}).get("children", [])]

    def _best_comment_story(self, post_id: str) -> dict[str, Any] | None:
        min_chars = int(self.story_config.get("min_story_chars", 450))
        min_comment_score = int(self.story_config.get("min_comment_score", 250))
        best: dict[str, Any] | None = None
        for comment in self._fetch_comments(post_id):
            body = self._usable_source_text(str(comment.get("body", "")))
            score = int(comment.get("score") or 0)
            if len(body) < min_chars or score < min_comment_score:
                continue
            if best is None or score > int(best.get("score") or 0):
                best = dict(comment)
        return best

    def _usable_source_text(self, value: str) -> str:
        text = clean_text(value)
        if text.lower() in {"[deleted]", "[removed]", "deleted", "removed"}:
            return ""
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        max_chars = int(self.story_config.get("max_source_chars", 7000))
        return text[:max_chars]

    def _priority_labels(self, subreddit: str, title: str, source_text: str) -> list[str]:
        text = f"{subreddit} {title} {source_text}".lower()
        categories = self.story_config.get("priority_keywords") or self._default_priority_keywords()
        labels: list[str] = []
        for label, keywords in categories.items():
            if any(str(keyword).lower() in text for keyword in keywords):
                labels.append(str(label))
        subreddit_defaults = {
            "letsnotmeet": "scary",
            "tifu": "funny_disaster",
            "todayilearned": "shocking_discovery",
            "interestingasfuck": "unbelievable",
            "askreddit": "life_changing",
            "nostupidquestions": "mystery",
            "lifeprotips": "life_changing",
        }
        default = subreddit_defaults.get(subreddit.lower())
        if default and default not in labels:
            labels.append(default)
        return labels[:4]

    def _default_priority_keywords(self) -> dict[str, list[str]]:
        return {
            "scary": [
                "scary",
                "creepy",
                "terrifying",
                "stalker",
                "break in",
                "followed",
                "alone",
                "heard",
                "shadow",
                "stranger",
            ],
            "unbelievable": [
                "unbelievable",
                "impossible",
                "insane",
                "wild",
                "no one believed",
                "one in a million",
                "odds",
            ],
            "mystery": [
                "mystery",
                "missing",
                "unknown",
                "found",
                "secret",
                "hidden",
                "clue",
                "never explained",
            ],
            "life_changing": [
                "changed my life",
                "saved",
                "realized",
                "woke up",
                "decision",
                "quit",
                "lost everything",
                "started over",
            ],
            "funny_disaster": [
                "tifu",
                "embarrassing",
                "accidentally",
                "disaster",
                "ruined",
                "boss",
                "wedding",
                "mistake",
            ],
            "shocking_discovery": [
                "discovered",
                "found out",
                "secret",
                "dna",
                "camera",
                "hidden",
                "learned",
                "today i learned",
            ],
        }

    def _selection_score(
        self,
        subreddit: str,
        source_text: str,
        ups: int,
        comments: int,
        upvote_ratio: float,
        created_utc: float,
        priority_labels: list[str],
        comment_score: int = 0,
    ) -> float:
        age_hours = max(1.0, (time.time() - created_utc) / 3600)
        engagement = ups + comments * 5 + comment_score * 2
        text_words = len(source_text.split())
        length_fit = 1.0
        if text_words < 120:
            length_fit = 0.72
        elif text_words > 1400:
            length_fit = 0.82
        category_boost = 1.0 + min(0.45, len(priority_labels) * 0.11)
        scary_bonus = 1.12 if "scary" in priority_labels or subreddit.lower() == "letsnotmeet" else 1.0
        freshness_boost = max(0.55, min(1.2, 1.2 - (age_hours / 24 / 30) * 0.2))
        ratio_boost = max(0.7, min(1.08, upvote_ratio or 0.86))
        return engagement * length_fit * category_boost * scary_bonus * freshness_boost * ratio_boost

    def topic_from_candidate(self, candidate: RedditStoryCandidate) -> TrendTopic:
        story_report = self.story_source_report_from_candidate(candidate)
        raw = {
            "content_mode": "reddit_story",
            "story_source": story_report,
            "source_text_for_similarity": candidate.source_text,
            "source_text_hash": candidate.source_hash,
            "source_text_word_count": len(candidate.source_text.split()),
            "story_beats": self.extract_core_narrative(candidate.source_text, candidate.priority_labels),
            "claim_status": "original_story_seed" if candidate.source_kind == "original_story_seed" else "unverified_reddit_story",
        }
        return TrendTopic(
            source=self.source_name,
            title=self._topic_title(candidate),
            url=candidate.source_url,
            score=candidate.selection_score,
            niche="reddit_story",
            volume=candidate.ups + candidate.comments,
            engagement=round(candidate.ups + candidate.comments * 5, 2),
            hashtags=["redditstories", "storytime", "shorts", *candidate.priority_labels[:3]],
            raw=raw,
            observed_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )

    def extract_core_narrative(self, source_text: str, labels: list[str] | None = None) -> dict[str, Any]:
        sentences = split_sentences(source_text)
        clean_sentences = [self._abstract_sentence(sentence) for sentence in sentences if len(sentence.split()) >= 5]
        if not clean_sentences:
            clean_sentences = [self._abstract_sentence(source_text)]
        first = clean_sentences[0]
        middle = clean_sentences[len(clean_sentences) // 2]
        ending = clean_sentences[-1]
        return {
            "source": "reddit",
            "claim_status": "unverified_reddit_story",
            "priority_labels": list(labels or []),
            "hook_seed": self._short_abstract(first, 12),
            "curiosity": self._short_abstract(first if len(clean_sentences) == 1 else clean_sentences[1], 18),
            "escalation": self._short_abstract(middle, 18),
            "reveal": self._short_abstract(ending, 18),
            "ending": "Frame the ending as the poster's claimed experience, not verified fact.",
        }

    def _abstract_sentence(self, sentence: str) -> str:
        text = clean_text(sentence)
        text = re.sub(r"\bI\b", "the poster", text)
        text = re.sub(r"\bmy\b", "their", text, flags=re.IGNORECASE)
        text = re.sub(r"\bme\b", "them", text, flags=re.IGNORECASE)
        text = re.sub(r"\bwe\b", "they", text, flags=re.IGNORECASE)
        text = re.sub(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", "an email", text)
        text = re.sub(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", "a phone number", text)
        return text

    def _short_abstract(self, text: str, max_words: int) -> str:
        words = clean_text(text).split()
        return " ".join(words[:max_words]).rstrip(" ,;:.") if words else ""

    def story_source_report(self, topic: TrendTopic) -> dict[str, Any]:
        raw = topic.raw or {}
        report = dict(raw.get("story_source") or {})
        if not report:
            report = {
                "content_mode": "reddit_story",
                "source_url": topic.url,
                "source_title": topic.title,
                "audit_warning": "story source metadata was missing from topic payload",
            }
        report["raw_source_text_stored_in_report"] = False
        report["script_copy_policy"] = "Do not read or copy Reddit post/comment text verbatim."
        report["fiction_framing_policy"] = "Present as an unverified Reddit story unless independently verified."
        return report

    def story_source_report_from_candidate(self, candidate: RedditStoryCandidate) -> dict[str, Any]:
        is_original_fallback = candidate.source_kind == "original_story_seed"
        return {
            "content_mode": "reddit_story",
            "source_platform": candidate.source_platform,
            "source_subreddit": candidate.subreddit,
            "source_url": candidate.source_url,
            "source_kind": candidate.source_kind,
            "post_id": candidate.post_id,
            "comment_id": candidate.comment_id,
            "source_title": candidate.title,
            "source_created_at": candidate.created_at_iso,
            "upvotes": candidate.ups,
            "comments": candidate.comments,
            "upvote_ratio": round(candidate.upvote_ratio, 3),
            "comment_score": candidate.comment_score,
            "minimum_upvote_threshold": int(self.story_config.get("min_upvotes", 2500)),
            "minimum_comment_threshold": int(self.story_config.get("min_comments", 120)),
            "engagement_threshold_met": (candidate.ups >= int(self.story_config.get("min_upvotes", 2500)) and candidate.comments >= int(self.story_config.get("min_comments", 120))) if not is_original_fallback else None,
            "fallback_reason": candidate.fallback_reason,
            "priority_labels": list(candidate.priority_labels),
            "selection_score": round(candidate.selection_score, 2),
            "source_text_sha256": candidate.source_hash,
            "source_text_word_count": len(candidate.source_text.split()),
            "raw_source_text_included": False,
            "approved_content_sources": list(self.story_config.get("subreddits", [])),
            "royalty_free_background_mode": self.config.get("story_mode", {})
            .get("background_video", {})
            .get("mode", "custom_background_library"),
            "approved_background_categories": {
                "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
                "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
            },
            "freshness_window_hours": int(self.story_config.get("freshness_window_hours", 168)),
            "trust_validation": (
                "original ShortMaster-owned story seed used only after Reddit source failure; do not present as fact"
                if is_original_fallback
                else "auditable Reddit URL with engagement thresholds; anecdote must be framed as unverified"
            ),
        }

    def build_research_brief(self, topic: TrendTopic) -> ResearchBrief:
        report = self.story_source_report(topic)
        created_at = clean_text(str(report.get("source_created_at", "")))
        freshness_window = int(self.story_config.get("freshness_window_hours", 168))
        freshness_threshold = float(self.story_config.get("freshness_min_score", 70))
        freshness_score, freshness_notes = self._freshness_score(created_at, freshness_window)
        trust_score, trust_notes = self._story_trust_score(report)
        is_original_fallback = report.get("source_kind") == "original_story_seed"
        if is_original_fallback:
            facts = [
                "ShortMaster selected an original story seed after live Reddit fetching failed.",
                "The seed is owned by ShortMaster and is safe to rewrite for entertainment.",
                "The public script must not claim the story is verified or sourced from a Reddit user.",
            ]
        else:
            facts = [
                (
                    f"Reddit story source is r/{report.get('source_subreddit', 'unknown')} "
                    f"with URL {report.get('source_url', topic.url)}."
                ),
                (
                    f"Reddit engagement passed thresholds with {report.get('upvotes', 0)} upvotes "
                    f"and {report.get('comments', 0)} comments."
                ),
                (
                    "The script must frame the source as an unverified Reddit story and rewrite the narrative from scratch."
                ),
            ]
        labels = report.get("priority_labels") or []
        if labels:
            facts.append("Story priority labels include " + ", ".join(str(label) for label in labels[:4]) + ".")
        source_summary = {
            "source": "ShortMaster original story seed" if is_original_fallback else "Reddit",
            "title": clean_text(str(report.get("source_title", topic.title))),
            "summary": (
                "Original internal story seed selected because Reddit fetch failed; no Reddit user text is copied."
                if is_original_fallback
                else "Auditable Reddit story source selected by engagement, comments, source subreddit, "
                "and retention category. Raw Reddit text is not included in generated reports."
            ),
            "url": clean_text(str(report.get("source_url", topic.url))),
            "source_trust_score": round(trust_score, 1),
            "source_trust_reason": (
                "Original owned story seed; safe for entertainment but not a factual claim."
                if is_original_fallback
                else "Reddit anecdote is auditable but unverified; script requires explicit story framing."
            ),
        }
        if created_at:
            source_summary["source_date"] = created_at
        source_dates = []
        if created_at:
            source_dates.append(
                {
                    "source": "ShortMaster original story seed" if is_original_fallback else "Reddit",
                    "title": clean_text(str(report.get("source_title", topic.title))),
                    "url": clean_text(str(report.get("source_url", topic.url))),
                    "source_date": created_at,
                    "source_trust_score": round(trust_score, 1),
                }
            )
        return ResearchBrief(
            topic=topic.title,
            category="reddit_story",
            why_now=(
                "Reddit story mode selected this source because it passed engagement thresholds "
                "and matched high-retention story categories."
            ),
            concrete_facts=facts,
            names_entities=["Reddit", f"r/{report.get('source_subreddit', 'unknown')}"],
            dates_times=[created_at] if created_at else [],
            locations=[],
            uncertainty_notes=[
                (
                    "Fallback story seed is original entertainment material; narration must not present it as verified fact."
                    if is_original_fallback
                    else "Reddit anecdotes are not independently verified; narration must not present them as fact."
                ),
                "Reddit source text and comments must never be copied verbatim.",
            ],
            source_urls=[clean_text(str(report.get("source_url", topic.url)))],
            source_summaries=[source_summary],
            source_dates=source_dates,
            freshness_score=freshness_score,
            freshness_threshold=freshness_threshold,
            freshness_window_hours=freshness_window,
            requires_fresh_source=bool(self.story_config.get("requires_fresh_source", True)),
            freshness_notes=freshness_notes,
            trust_score=trust_score,
            trust_threshold=float(self.story_config.get("trust_min_score", 70)),
            requires_trusted_source=False,
            trust_notes=trust_notes,
        )

    def _freshness_score(self, created_at: str, freshness_window_hours: int) -> tuple[float, list[str]]:
        if not self.story_config.get("requires_fresh_source", True):
            return 100.0, ["story freshness gate disabled by configuration"]
        if not created_at:
            return 0.0, ["Reddit story source has no created_at timestamp"]
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0, [f"Reddit source timestamp could not be parsed: {created_at}"]
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600)
        if age_hours <= freshness_window_hours:
            return round(min(100.0, 100.0 - age_hours * 0.12), 1), [
                f"Reddit story source is {age_hours:.1f} hours old within {freshness_window_hours}h window"
            ]
        score = max(0.0, 69.0 - (age_hours - freshness_window_hours) * 0.04)
        return round(score, 1), [
            f"Reddit story source is {age_hours:.1f} hours old; target window is {freshness_window_hours}h"
        ]

    def _story_trust_score(self, report: dict[str, Any]) -> tuple[float, list[str]]:
        if report.get("source_kind") == "original_story_seed":
            return 95.0, [
                "Original ShortMaster story seed is owned/internal and avoids unverifiable factual claims."
            ]
        notes = ["Reddit URL is auditable; anecdote remains unverified and must be framed as a story."]
        score = 72.0
        if report.get("source_url"):
            score += 10
        if int(report.get("upvotes") or 0) >= int(self.story_config.get("min_upvotes", 2500)):
            score += 7
        if int(report.get("comments") or 0) >= int(self.story_config.get("min_comments", 120)):
            score += 6
        if report.get("priority_labels"):
            score += 4
        return round(min(96.0, score), 1), notes

    def _candidate_rejection_report(self, candidate: RedditStoryCandidate) -> dict[str, Any]:
        return {
            "subreddit": candidate.subreddit,
            "post_id": candidate.post_id,
            "source_url": candidate.source_url,
            "title": candidate.title,
            "upvotes": candidate.ups,
            "comments": candidate.comments,
            "priority_labels": candidate.priority_labels,
            "rejection_reasons": candidate.rejection_reasons,
        }

    def _reddit_oauth_token(self) -> str:
        if not self._official_api_enabled():
            return ""
        if self._oauth_token and time.time() < self._oauth_token_expires_at - 60:
            return self._oauth_token
        client_id = os.getenv(str(self.story_config.get("client_id_env", "REDDIT_CLIENT_ID")))
        client_secret = os.getenv(str(self.story_config.get("client_secret_env", "REDDIT_CLIENT_SECRET")))
        if not client_id or not client_secret:
            return ""
        response = self.session.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=HTTPBasicAuth(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self._user_agent()},
            timeout=float(self.story_config.get("timeout_seconds", 20)),
        )
        if response.status_code == 429:
            raise requests.RequestException("Reddit OAuth token rate limit reached")
        response.raise_for_status()
        payload = response.json()
        token = clean_text(str(payload.get("access_token", "")))
        if not token:
            raise requests.RequestException("Reddit OAuth token response did not include access_token")
        self._oauth_token = token
        self._oauth_token_expires_at = time.time() + float(payload.get("expires_in") or 3600)
        return self._oauth_token

    def _official_api_enabled(self) -> bool:
        return bool(self.story_config.get("official_api_enabled", True))

    def _original_fallback_enabled(self) -> bool:
        return bool(self.story_config.get("allow_original_story_fallback", True))

    def _record_source_failure(self, subreddit: str, source_type: str, reason: str) -> None:
        self.last_source_failures.append(
            {
                "subreddit": subreddit,
                "source_type": source_type,
                "reason": reason,
            }
        )

    def _user_agent(self) -> str:
        return str(
            self.story_config.get(
                "user_agent",
                "ShortsMasterBot/1.0 reddit story shorts mode",
            )
        )

    def _fallback_candidates(self) -> list[RedditStoryCandidate]:
        seed = self._fallback_story_seed()
        now = time.time()
        failure_summary = "; ".join(
            f"{item['subreddit']}:{item['source_type']}:{item['reason']}"
            for item in self.last_source_failures[:8]
        )
        reason = (
            "Live Reddit source fetch failed or returned no eligible stories"
            + (f" ({failure_summary})" if failure_summary else "")
        )
        candidate = RedditStoryCandidate(
            subreddit="original-story-seed",
            post_id=seed["id"],
            title=seed["title"],
            source_text=seed["source_text"],
            source_url=f"internal://shortmaster/original-story-seeds/{seed['id']}",
            source_kind="original_story_seed",
            ups=0,
            comments=0,
            upvote_ratio=1.0,
            created_utc=now,
            priority_labels=seed["priority_labels"],
            selection_score=120_000.0,
            source_platform="ShortMaster",
            fallback_reason=reason,
        )
        LOGGER.warning("Using original Reddit-style story fallback: %s", reason)
        return [candidate]

    def _fallback_story_seed(self) -> dict[str, Any]:
        seeds = [
            {
                "id": "night-shift-mall-001",
                "title": "A ligação estranha no shopping vazio",
                "priority_labels": ["scary", "mystery"],
                "source_text": (
                    "Original story seed: mall encounter, overnight security shift, stranger asks for help, "
                    "phone call describes the narrator, footsteps in a closed corridor, final discovery on camera. "
                    "Rewrite in Brazilian Portuguese with suspense; do not claim it is verified."
                ),
            },
            {
                "id": "wrong-apartment-key-002",
                "title": "A chave que abriu o apartamento errado",
                "priority_labels": ["mystery", "unbelievable"],
                "source_text": (
                    "Original story seed: tired renter receives a spare key, opens the wrong apartment, finds photos "
                    "of their own hallway, hears someone coming upstairs, and realizes the key was left deliberately. "
                    "Rewrite from scratch in Brazilian Portuguese."
                ),
            },
            {
                "id": "family-photo-box-003",
                "title": "A caixa escondida na reforma",
                "priority_labels": ["shocking_discovery", "life_changing"],
                "source_text": (
                    "Original story seed: family renovation, hidden box behind furniture, old photo, unknown relative, "
                    "safe deposit key, emotional reveal. Localize naturally for Brazilian audiences."
                ),
            },
        ]
        index = int(datetime.now(timezone.utc).strftime("%j")) % len(seeds)
        return seeds[index]

    def _topic_title(self, candidate: RedditStoryCandidate) -> str:
        if candidate.source_kind == "original_story_seed":
            return f"Reddit-style original story seed: {candidate.title}"
        return f"Reddit story from r/{candidate.subreddit}: {candidate.title}"


def is_reddit_story_topic(topic: TrendTopic) -> bool:
    return topic.source == RedditStoryService.source_name or (topic.raw or {}).get("content_mode") == "reddit_story"
