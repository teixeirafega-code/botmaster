from __future__ import annotations

import html

import requests

from app.config.settings import TelegramSettings
from app.models import OpportunityReport
from app.utils.logger import get_logger


logger = get_logger(__name__)


class TelegramNotifier:
    def __init__(self, settings: TelegramSettings, paper_mode: bool) -> None:
        self.settings = settings
        self.paper_mode = paper_mode

    def send_opportunity_alert(self, report: OpportunityReport) -> bool:
        if not self.settings.enabled:
            return False

        message = self._format_message(report)
        if not self.settings.bot_token or not self.settings.chat_id:
            if self.paper_mode:
                logger.info("Telegram paper alert:\n%s", message)
                return True
            logger.error("Telegram credentials are missing; alert not sent")
            return False

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessage",
                json={
                    "chat_id": self.settings.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.exception("Telegram alert failed: %s", exc)
            return False

    def _format_message(self, report: OpportunityReport) -> str:
        trend = report.trend
        domains = "\n".join(
            f"- {html.escape(action.domain)}: {html.escape(action.action)} ({html.escape(action.reason)})"
            for action in report.domain_actions
        ) or "- domain automation disabled"
        handles = "\n".join(
            f"- {html.escape(platform)}: {html.escape(handle)}"
            for platform, handle in report.social_handles.items()
        )
        ideas = "\n".join(
            f"- {html.escape(idea.channel)}: {html.escape(idea.title)}"
            for idea in report.content_ideas[:3]
        )
        components = ", ".join(
            f"{name}={value:.1f}" for name, value in trend.component_scores.items()
        )
        return (
            f"<b>Trend Hunter Alert</b>\n"
            f"<b>Trend:</b> {html.escape(trend.name)}\n"
            f"<b>Score:</b> {trend.score:.1f}/100\n"
            f"<b>Platforms:</b> {html.escape(', '.join(trend.platforms))}\n"
            f"<b>Components:</b> {html.escape(components)}\n\n"
            f"<b>Domain actions</b>\n{domains}\n\n"
            f"<b>Social profile names</b>\n{handles}\n\n"
            f"<b>Content ideas</b>\n{ideas}"
        )

