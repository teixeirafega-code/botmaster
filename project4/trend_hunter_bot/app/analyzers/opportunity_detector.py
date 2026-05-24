from __future__ import annotations

from app.actions.content_generator import ContentGenerator
from app.actions.domain_registrar import DomainRegistrar
from app.config.settings import Settings
from app.models import OpportunityReport, ScoredTrend, slugify_trend_name


class OpportunityDetector:
    def __init__(
        self,
        settings: Settings,
        domain_registrar: DomainRegistrar,
        content_generator: ContentGenerator,
    ) -> None:
        self.settings = settings
        self.domain_registrar = domain_registrar
        self.content_generator = content_generator

    def detect_and_act(self, trend: ScoredTrend) -> OpportunityReport | None:
        if trend.score < self.settings.app.trend_score_threshold:
            return None

        domain_actions = ()
        if self.settings.domains.enabled:
            candidates = self._domain_candidates(trend)
            domain_actions = self.domain_registrar.process_candidates(candidates)

        social_handles = self._social_handles(trend)
        content_ideas = self.content_generator.generate(trend)
        action_taken = any(action.action in {"paper_registered", "registered"} for action in domain_actions)

        return OpportunityReport(
            trend=trend,
            domain_actions=domain_actions,
            social_handles=social_handles,
            content_ideas=content_ideas,
            action_taken=action_taken,
        )

    def _domain_candidates(self, trend: ScoredTrend) -> tuple[str, ...]:
        slug = slugify_trend_name(trend.normalized_name)
        variants = [slug]
        if len(slug) <= 42:
            variants.extend([f"get{slug}", f"{slug}hub", f"{slug}guide"])

        candidates: list[str] = []
        for variant in variants:
            for tld in self.settings.domains.tlds:
                candidates.append(f"{variant}.{tld.lstrip('.')}")
                if len(candidates) >= self.settings.domains.max_candidates_per_trend:
                    return tuple(candidates)
        return tuple(candidates)

    def _social_handles(self, trend: ScoredTrend) -> dict[str, str]:
        slug = slugify_trend_name(trend.normalized_name, max_length=20)
        handles: dict[str, str] = {}
        for platform in self.settings.domains.social_platforms:
            prefix = "" if platform == "x" else "@"
            handles[platform] = f"{prefix}{slug}"
        return handles

