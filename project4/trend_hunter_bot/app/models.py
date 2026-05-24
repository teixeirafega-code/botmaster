from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_trend_name(name: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(tokens).strip()


def slugify_trend_name(name: str, max_length: int = 48) -> str:
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    slug = "".join(tokens)
    return slug[:max_length] or "trend"


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class TrendSignal:
    name: str
    platform: str
    observed_at: datetime = field(default_factory=utc_now)
    growth_velocity: float = 0.0
    search_volume: float = 0.0
    social_engagement: float = 0.0
    url: str | None = None
    keywords: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized_name(self) -> str:
        return normalize_trend_name(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "platform": self.platform,
            "observed_at": self.observed_at.isoformat(),
            "growth_velocity": self.growth_velocity,
            "search_volume": self.search_volume,
            "social_engagement": self.social_engagement,
            "url": self.url,
            "keywords": list(self.keywords),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ScoredTrend:
    name: str
    normalized_name: str
    score: float
    component_scores: dict[str, float]
    signals: tuple[TrendSignal, ...]
    detected_at: datetime = field(default_factory=utc_now)

    @property
    def platforms(self) -> tuple[str, ...]:
        return tuple(sorted({signal.platform for signal in self.signals}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "score": self.score,
            "component_scores": self.component_scores,
            "platforms": list(self.platforms),
            "detected_at": self.detected_at.isoformat(),
            "signals": [signal.to_dict() for signal in self.signals],
        }


@dataclass(frozen=True)
class ContentIdea:
    title: str
    channel: str
    angle: str
    call_to_action: str

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "channel": self.channel,
            "angle": self.angle,
            "call_to_action": self.call_to_action,
        }


@dataclass(frozen=True)
class DomainAction:
    domain: str
    available: bool | None
    action: str
    mode: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "available": self.available,
            "action": self.action,
            "mode": self.mode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OpportunityReport:
    trend: ScoredTrend
    domain_actions: tuple[DomainAction, ...]
    social_handles: dict[str, str]
    content_ideas: tuple[ContentIdea, ...]
    action_taken: bool
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend": self.trend.to_dict(),
            "domain_actions": [action.to_dict() for action in self.domain_actions],
            "social_handles": self.social_handles,
            "content_ideas": [idea.to_dict() for idea in self.content_ideas],
            "action_taken": self.action_taken,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class CycleSummary:
    started_at: datetime
    finished_at: datetime
    signals_collected: int
    trends_scored: int
    opportunities_detected: int
    alerts_sent: int
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "signals_collected": self.signals_collected,
            "trends_scored": self.trends_scored,
            "opportunities_detected": self.opportunities_detected,
            "alerts_sent": self.alerts_sent,
            "errors": list(self.errors),
        }

