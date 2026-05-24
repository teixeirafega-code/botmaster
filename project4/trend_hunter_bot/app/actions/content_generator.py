from __future__ import annotations

from app.config.settings import ContentSettings
from app.models import ContentIdea, ScoredTrend


class ContentGenerator:
    def __init__(self, settings: ContentSettings) -> None:
        self.settings = settings

    def generate(self, trend: ScoredTrend) -> tuple[ContentIdea, ...]:
        platforms = ", ".join(trend.platforms)
        score = int(round(trend.score))
        ideas = [
            ContentIdea(
                title=f"What {trend.name} means before the market catches up",
                channel="blog",
                angle=(
                    f"Explain why {trend.name} is accelerating across {platforms}, "
                    f"then map the practical use cases for {self.settings.audience}."
                ),
                call_to_action="Invite readers to join an early-access trend watchlist.",
            ),
            ContentIdea(
                title=f"{trend.name}: 5 fast opportunities to test this week",
                channel="newsletter",
                angle=(
                    f"Turn the trend score of {score}/100 into concrete experiments, "
                    "including domain plays, affiliate angles, and short-form content hooks."
                ),
                call_to_action="Ask subscribers to vote on the highest-conviction experiment.",
            ),
            ContentIdea(
                title=f"Is {trend.name} hype or a real demand spike?",
                channel="social_thread",
                angle=(
                    "Compare search momentum, social engagement, and commercial intent "
                    "with a concise verdict and next-step checklist."
                ),
                call_to_action="Drive replies with a request for overlooked data points.",
            ),
            ContentIdea(
                title=f"60-second explainer: why everyone is watching {trend.name}",
                channel="short_video",
                angle=(
                    "Open with the fastest-moving signal, show two examples, and end with "
                    "one practical way to act before saturation."
                ),
                call_to_action="Send viewers to a saved resource page or waitlist.",
            ),
            ContentIdea(
                title=f"The beginner's guide to monetizing {trend.name}",
                channel="blog",
                angle=(
                    "Package the trend into buyer personas, search keywords, productized "
                    "service ideas, and simple validation steps."
                ),
                call_to_action="Offer a downloadable validation worksheet.",
            ),
            ContentIdea(
                title=f"{trend.name} domain and handle ideas worth checking now",
                channel="social_thread",
                angle=(
                    "Share memorable brand-name patterns and explain why short, exact-match, "
                    "and action-oriented names can capture demand."
                ),
                call_to_action="Invite followers to request a name audit.",
            ),
            ContentIdea(
                title=f"How founders can ride {trend.name} without chasing noise",
                channel="newsletter",
                angle=(
                    "Separate immediate audience demand from durable business value, then "
                    "rank the lowest-risk products or lead magnets to ship."
                ),
                call_to_action="Offer a teardown of one reader's trend idea.",
            ),
            ContentIdea(
                title=f"Three hooks that make {trend.name} instantly understandable",
                channel="short_video",
                angle=(
                    "Test curiosity, contrarian, and practical-benefit hooks against the "
                    "trend's strongest platform signal."
                ),
                call_to_action="Ask viewers to save the hook template for later use.",
            ),
        ]

        allowed_channels = set(self.settings.channels)
        filtered = [idea for idea in ideas if idea.channel in allowed_channels]
        if not filtered:
            filtered = ideas
        return tuple(filtered[: self.settings.max_ideas])

