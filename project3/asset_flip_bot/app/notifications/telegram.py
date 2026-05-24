from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config.settings import TelegramSettings
from app.models import ScoredOpportunity
from app.utils.logger import get_logger


class TelegramNotifier:
    def __init__(self, settings: TelegramSettings | None, paper_mode: bool = True) -> None:
        self.settings = settings
        self.paper_mode = paper_mode
        self.logger = get_logger("notifications.telegram")

    def send_opportunity(self, opportunity: ScoredOpportunity) -> bool:
        message = self.format_opportunity(opportunity)
        if self.paper_mode:
            self.logger.info("PAPER MODE Telegram alert:\n%s", message)
            return True
        if not self.settings or not self.settings.enabled:
            self.logger.info("Telegram disabled. Alert not sent for %s", opportunity.listing.name)
            return False
        if not self.settings.bot_token or not self.settings.chat_id:
            self.logger.warning("Telegram credentials are missing. Alert not sent.")
            return False

        endpoint = f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessage"
        payload = {
            "chat_id": self.settings.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            ok = bool(body.get("ok"))
            if ok:
                self.logger.info("Telegram alert sent for %s", opportunity.listing.name)
            else:
                self.logger.error("Telegram API rejected alert: %s", body)
            return ok
        except (HTTPError, URLError, TimeoutError) as exc:
            self.logger.exception("Telegram alert failed: %s", exc)
            return False

    def format_opportunity(self, opportunity: ScoredOpportunity) -> str:
        listing = opportunity.listing
        valuation = opportunity.valuation
        lines = [
            "<b>Asset Flip Opportunity</b>",
            f"<b>{self._escape(listing.name)}</b>",
            f"Marketplace: {self._escape(listing.marketplace)}",
            f"Asking Price: ${listing.asking_price:,.0f}",
            f"Estimated Real Value: ${valuation.estimated_real_value:,.0f}",
            f"Profit Potential: ${valuation.profit_potential:,.0f}",
            f"Opportunity Score: {opportunity.opportunity_score}/100",
            f"Revenue: ${max(listing.monthly_profit, listing.monthly_revenue):,.0f}/mo",
            f"Asset Type: {self._escape(listing.asset_type.value)}",
            f"Niche: {self._escape(listing.niche)}",
            f"URL: {self._escape(listing.url)}",
        ]
        if opportunity.reasons:
            lines.append("Signals: " + self._escape("; ".join(opportunity.reasons[:4])))
        return "\n".join(lines)

    def _escape(self, value: str) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
