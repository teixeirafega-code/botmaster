from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean

from app.config.settings import ScoringSettings
from app.models import ScoredTrend, TrendSignal, normalize_trend_name


class TrendScorer:
    def __init__(self, settings: ScoringSettings) -> None:
        self.settings = settings

    def group_signals(self, signals: list[TrendSignal]) -> dict[str, list[TrendSignal]]:
        grouped: dict[str, list[TrendSignal]] = defaultdict(list)
        for signal in signals:
            normalized = normalize_trend_name(signal.name)
            if normalized:
                grouped[normalized].append(signal)
        return dict(grouped)

    def score_signals(self, signals: list[TrendSignal]) -> list[ScoredTrend]:
        scored = []
        for normalized_name, grouped_signals in self.group_signals(signals).items():
            scored.append(self.score_group(normalized_name, grouped_signals))
        return sorted(scored, key=lambda trend: trend.score, reverse=True)

    def score_group(self, normalized_name: str, signals: list[TrendSignal]) -> ScoredTrend:
        if not signals:
            raise ValueError("signals must not be empty")

        growth = self._bounded_average(
            [signal.growth_velocity for signal in signals],
            self.settings.max_growth_velocity,
        )
        search = self._bounded_average(
            [signal.search_volume for signal in signals],
            self.settings.max_search_volume,
        )
        engagement = self._log_scaled_average(
            [signal.social_engagement for signal in signals],
            self.settings.max_social_engagement,
        )
        commercial = self._commercial_score(signals)

        raw_components = {
            "growth_velocity": growth,
            "search_volume": search,
            "social_engagement": engagement,
            "commercial_potential": commercial,
        }

        weight_sum = sum(self.settings.weights.values()) or 1.0
        weighted = sum(
            raw_components[name] * self.settings.weights.get(name, 0.0)
            for name in raw_components
        ) / weight_sum

        platform_count = len({signal.platform for signal in signals})
        platform_boost = min(
            max(platform_count - 1, 0) * self.settings.platform_boost_per_extra_platform,
            self.settings.max_platform_boost,
        )
        score = max(0.0, min(100.0, weighted + platform_boost))

        display_name = self._choose_display_name(signals)
        return ScoredTrend(
            name=display_name,
            normalized_name=normalized_name,
            score=round(score, 2),
            component_scores={key: round(value, 2) for key, value in raw_components.items()},
            signals=tuple(signals),
        )

    def _bounded_average(self, values: list[float], maximum: float) -> float:
        if not values or maximum <= 0:
            return 0.0
        positives = [max(value, 0.0) for value in values]
        return max(0.0, min(100.0, mean(positives) / maximum * 100.0))

    def _log_scaled_average(self, values: list[float], maximum: float) -> float:
        if not values or maximum <= 1:
            return 0.0
        positives = [max(value, 0.0) for value in values]
        average = mean(positives)
        return max(0.0, min(100.0, math.log1p(average) / math.log1p(maximum) * 100.0))

    def _commercial_score(self, signals: list[TrendSignal]) -> float:
        terms = set(self.settings.commercial_keywords)
        emerging = set(self.settings.emerging_keywords)
        text_parts: list[str] = []
        for signal in signals:
            text_parts.append(signal.name)
            text_parts.extend(signal.keywords)
            text_parts.extend(str(value) for value in signal.metadata.values())
        text = " ".join(text_parts).lower()

        commercial_hits = sum(1 for term in terms if term in text)
        emerging_hits = sum(1 for term in emerging if term in text)
        source_diversity = len({signal.platform for signal in signals})

        score = commercial_hits * 10.0 + emerging_hits * 5.0 + source_diversity * 8.0
        return max(0.0, min(100.0, score))

    def _choose_display_name(self, signals: list[TrendSignal]) -> str:
        return max(
            signals,
            key=lambda signal: (
                signal.search_volume + signal.social_engagement + signal.growth_velocity,
                len(signal.name),
            ),
        ).name
