from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient


class DropCatchClient:
    name = "dropcatch"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def place_backorder(self, domain: str, max_bid: float | None = None) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {
                "domain": domain,
                "provider": self.name,
                "paper_mode": True,
                "max_bid": max_bid,
                "created_at": datetime.now(UTC).isoformat(),
            }
        if not self.settings.dropcatch_api_key:
            raise RuntimeError("DropCatch API key is required when PAPER_MODE=false")
        headers = {"Authorization": f"Bearer {self.settings.dropcatch_api_key.get_secret_value()}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {"domain": domain}
        if max_bid is not None:
            payload["max_bid"] = max_bid

        async def submit() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(f"{self.settings.dropcatch_base_url.rstrip('/')}/backorders", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("dropcatch", submit, policy=RetryPolicy(attempts=3, base_delay_seconds=0.5, timeout_seconds=10.0))
