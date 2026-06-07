from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.utils.text import normalize_topic_key


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class TrendTopic:
    source: str
    title: str
    score: float
    niche: str = "general"
    url: str = ""
    volume: int = 0
    engagement: float = 0.0
    hashtags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    observed_at: str = field(default_factory=utc_now_iso)

    @property
    def key(self) -> str:
        return normalize_topic_key(self.title)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "score": float(self.score),
            "niche": self.niche,
            "url": self.url,
            "volume": int(self.volume or 0),
            "engagement": float(self.engagement or 0.0),
            "hashtags": list(self.hashtags),
            "raw": dict(self.raw),
            "observed_at": self.observed_at,
            "key": self.key,
        }


@dataclass(slots=True)
class ContentScript:
    title: str
    hook: str
    narration: str
    scenes: list[dict[str, str]]
    tags: list[str]
    description: str
    niche: str
    engagement_prompt: str = ""
    engagement_prompt_type: str = ""
    engagement_score: float = 0.0
    engagement_prompt_variants: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "hook": self.hook,
            "narration": self.narration,
            "scenes": self.scenes,
            "tags": self.tags,
            "description": self.description,
            "niche": self.niche,
            "engagement_prompt": self.engagement_prompt,
            "engagement_prompt_type": self.engagement_prompt_type,
            "engagement_score": round(float(self.engagement_score), 1),
            "engagement_prompt_variants": [
                dict(item) for item in self.engagement_prompt_variants
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentScript":
        return cls(
            title=str(data["title"]),
            hook=str(data["hook"]),
            narration=str(data["narration"]),
            scenes=[dict(scene) for scene in data["scenes"]],
            tags=[str(tag) for tag in data["tags"]],
            description=str(data["description"]),
            niche=str(data["niche"]),
            engagement_prompt=str(data.get("engagement_prompt", "")),
            engagement_prompt_type=str(data.get("engagement_prompt_type", "")),
            engagement_score=float(data.get("engagement_score", 0.0) or 0.0),
            engagement_prompt_variants=[
                dict(item) for item in data.get("engagement_prompt_variants", [])
            ],
        )


@dataclass(slots=True)
class ResearchBrief:
    topic: str
    category: str
    why_now: str
    concrete_facts: list[str]
    names_entities: list[str] = field(default_factory=list)
    dates_times: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    uncertainty_notes: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_summaries: list[dict[str, str]] = field(default_factory=list)
    source_dates: list[dict[str, str]] = field(default_factory=list)
    freshness_score: float = 100.0
    freshness_threshold: float = 70.0
    freshness_window_hours: int = 72
    requires_fresh_source: bool = False
    freshness_notes: list[str] = field(default_factory=list)
    trust_score: float = 100.0
    trust_threshold: float = 70.0
    requires_trusted_source: bool = False
    trust_notes: list[str] = field(default_factory=list)

    @property
    def has_enough_facts(self) -> bool:
        return len(self.concrete_facts) >= 3

    def missing_facts_reason(self) -> str:
        found = len(self.concrete_facts)
        return f"Research found {found}/3 required concrete facts for topic '{self.topic}'"

    @property
    def has_fresh_source(self) -> bool:
        return not self.requires_fresh_source or self.freshness_score >= self.freshness_threshold

    @property
    def needs_fresh_source(self) -> bool:
        return self.has_enough_facts and self.requires_fresh_source and not self.has_fresh_source

    @property
    def has_trusted_source(self) -> bool:
        return not self.requires_trusted_source or self.trust_score >= self.trust_threshold

    @property
    def needs_trusted_source(self) -> bool:
        return (
            self.has_enough_facts
            and self.has_fresh_source
            and self.requires_trusted_source
            and not self.has_trusted_source
        )

    def stale_source_reason(self) -> str:
        return (
            f"Current sports topic '{self.topic}' has freshness score "
            f"{self.freshness_score:.1f}/{self.freshness_threshold:.1f}; "
            f"requires a source from the last {self.freshness_window_hours} hours"
        )

    def untrusted_source_reason(self) -> str:
        return (
            f"Current sports topic '{self.topic}' has trust score "
            f"{self.trust_score:.1f}/{self.trust_threshold:.1f}; "
            "requires an official or recognized sports source"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "category": self.category,
            "why_now": self.why_now,
            "concrete_facts": list(self.concrete_facts),
            "names_entities": list(self.names_entities),
            "dates_times": list(self.dates_times),
            "locations": list(self.locations),
            "uncertainty_notes": list(self.uncertainty_notes),
            "source_urls": list(self.source_urls),
            "source_summaries": [dict(item) for item in self.source_summaries],
            "source_dates": [dict(item) for item in self.source_dates],
            "freshness_score": round(float(self.freshness_score), 1),
            "freshness_threshold": round(float(self.freshness_threshold), 1),
            "freshness_window_hours": int(self.freshness_window_hours),
            "requires_fresh_source": bool(self.requires_fresh_source),
            "has_fresh_source": self.has_fresh_source,
            "needs_fresh_source": self.needs_fresh_source,
            "freshness_notes": list(self.freshness_notes),
            "trust_score": round(float(self.trust_score), 1),
            "trust_threshold": round(float(self.trust_threshold), 1),
            "requires_trusted_source": bool(self.requires_trusted_source),
            "has_trusted_source": self.has_trusted_source,
            "needs_trusted_source": self.needs_trusted_source,
            "trust_notes": list(self.trust_notes),
            "has_enough_facts": self.has_enough_facts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchBrief":
        return cls(
            topic=str(data["topic"]),
            category=str(data.get("category", "general")),
            why_now=str(data.get("why_now", "")),
            concrete_facts=[str(item) for item in data.get("concrete_facts", [])],
            names_entities=[str(item) for item in data.get("names_entities", [])],
            dates_times=[str(item) for item in data.get("dates_times", [])],
            locations=[str(item) for item in data.get("locations", [])],
            uncertainty_notes=[str(item) for item in data.get("uncertainty_notes", [])],
            source_urls=[str(item) for item in data.get("source_urls", [])],
            source_summaries=[dict(item) for item in data.get("source_summaries", [])],
            source_dates=[dict(item) for item in data.get("source_dates", [])],
            freshness_score=float(data.get("freshness_score", 100.0)),
            freshness_threshold=float(data.get("freshness_threshold", 70.0)),
            freshness_window_hours=int(data.get("freshness_window_hours", 72)),
            requires_fresh_source=bool(data.get("requires_fresh_source", False)),
            freshness_notes=[str(item) for item in data.get("freshness_notes", [])],
            trust_score=float(data.get("trust_score", 100.0)),
            trust_threshold=float(data.get("trust_threshold", 70.0)),
            requires_trusted_source=bool(data.get("requires_trusted_source", False)),
            trust_notes=[str(item) for item in data.get("trust_notes", [])],
        )


class QueueStatus:
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    GENERATING = "generating"
    READY = "ready"
    NEEDS_RESEARCH = "needs_research"
    NEEDS_FRESH_SOURCE = "needs_fresh_source"
    NEEDS_TRUSTED_SOURCE = "needs_trusted_source"
    SAFETY_BLOCKED = "safety_blocked"
    PUBLISHED = "published"
    PAPER_PUBLISHED = "paper_published"
    FAILED = "failed"

    ACTIVE = {PENDING_APPROVAL, APPROVED, GENERATING, READY}
    TERMINAL = {
        PUBLISHED,
        PAPER_PUBLISHED,
        NEEDS_RESEARCH,
        NEEDS_FRESH_SOURCE,
        NEEDS_TRUSTED_SOURCE,
        SAFETY_BLOCKED,
        FAILED,
    }
