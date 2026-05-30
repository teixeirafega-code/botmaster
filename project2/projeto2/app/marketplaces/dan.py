from __future__ import annotations

from typing import Any

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient


class DanMarketplace:
    name = "dan"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def list_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True}
        if not self.settings.dan_api_key:
            raise RuntimeError("Dan API key is required when PAPER_MODE=false and Dan listing is enabled")
        headers = {"Authorization": f"Bearer {self.settings.dan_api_key.get_secret_value()}", "Content-Type": "application/json"}
        payload = {"domain": domain, "price": price, "currency": "USD", "buy_now": True}

        async def create_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(f"{self.settings.dan_base_url.rstrip('/')}/domains", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("dan", create_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))

    async def reprice_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True, "repriced": True}
        if not self.settings.dan_api_key:
            raise RuntimeError("Dan API key is required when PAPER_MODE=false and Dan repricing is enabled")
        headers = {"Authorization": f"Bearer {self.settings.dan_api_key.get_secret_value()}", "Content-Type": "application/json"}
        payload = {"price": price, "currency": "USD"}

        async def update_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.patch(f"{self.settings.dan_base_url.rstrip('/')}/domains/{domain}", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("dan", update_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))
