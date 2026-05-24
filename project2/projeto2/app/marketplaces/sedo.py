from __future__ import annotations

from typing import Any

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient


class SedoMarketplace:
    name = "sedo"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def list_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True}
        if not self.settings.sedo_api_key:
            raise RuntimeError("Sedo API key is required when PAPER_MODE=false")
        headers = {"Authorization": f"Bearer {self.settings.sedo_api_key.get_secret_value()}"}
        payload = {"domain": domain, "price": price, "currency": "USD"}
        async def create_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(f"{self.settings.sedo_base_url}/domains", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("sedo", create_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))
