from __future__ import annotations

from typing import Any

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient


class AfternicMarketplace:
    name = "afternic"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def list_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True}
        if not self.settings.afternic_api_key:
            raise RuntimeError("Afternic API key is required when PAPER_MODE=false")
        headers = {"Authorization": f"Bearer {self.settings.afternic_api_key.get_secret_value()}"}
        payload = {"domain": domain, "buy_now_price": price, "currency": "USD"}
        async def create_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(f"{self.settings.afternic_base_url}/listings", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("afternic", create_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))

    async def reprice_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True, "repriced": True}
        if not self.settings.afternic_api_key:
            raise RuntimeError("Afternic API key is required when PAPER_MODE=false")
        headers = {"Authorization": f"Bearer {self.settings.afternic_api_key.get_secret_value()}"}
        payload = {"domain": domain, "buy_now_price": price, "currency": "USD"}

        async def update_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.patch(f"{self.settings.afternic_base_url}/listings/{domain}", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("afternic", update_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))
