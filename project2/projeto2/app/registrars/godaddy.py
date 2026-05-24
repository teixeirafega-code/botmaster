from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient


class GoDaddyRegistrar:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def register(self, domain: str, years: int = 1) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {
                "domain": domain,
                "registrar": "godaddy",
                "paper_mode": True,
                "cost": 12.0,
                "registered_at": datetime.now(UTC).isoformat(),
            }
        if not self.settings.godaddy_api_key or not self.settings.godaddy_api_secret:
            raise RuntimeError("GoDaddy API credentials are required when PAPER_MODE=false")

        payload = {
            "domain": domain,
            "consent": {"agreedAt": datetime.now(UTC).isoformat(), "agreedBy": "Domain Hunter Bot"},
            "period": years,
            "renewAuto": False,
        }
        async def purchase() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.post(f"{self.settings.godaddy_base_url}/v1/domains/purchase", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("godaddy", purchase, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))

    def _headers(self) -> dict[str, str]:
        key = self.settings.godaddy_api_key.get_secret_value() if self.settings.godaddy_api_key else ""
        secret = self.settings.godaddy_api_secret.get_secret_value() if self.settings.godaddy_api_secret else ""
        return {"Authorization": f"sso-key {key}:{secret}", "Content-Type": "application/json"}
