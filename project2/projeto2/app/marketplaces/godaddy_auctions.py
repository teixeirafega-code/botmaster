from __future__ import annotations

from typing import Any

import aiohttp

from app.config.settings import Settings
from app.core.resilience import RetryPolicy, run_resilient


class GoDaddyAuctionsMarketplace:
    name = "godaddy_auctions"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def list_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True}
        key = self.settings.godaddy_api_key.get_secret_value() if self.settings.godaddy_api_key else ""
        secret = self.settings.godaddy_api_secret.get_secret_value() if self.settings.godaddy_api_secret else ""
        headers = {"Authorization": f"sso-key {key}:{secret}", "Content-Type": "application/json"}
        payload = {"domain": domain, "price": price, "type": "offer_counter_offer"}
        async def create_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(f"{self.settings.godaddy_base_url}/v1/aftermarket/listings", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("godaddy_auctions", create_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))

    async def reprice_domain(self, domain: str, price: int) -> dict[str, Any]:
        if self.settings.paper_mode:
            return {"marketplace": self.name, "domain": domain, "price": price, "paper_mode": True, "repriced": True}
        headers = self._headers()
        payload = {"domain": domain, "price": price}

        async def update_listing() -> dict[str, Any]:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.patch(f"{self.settings.godaddy_base_url}/v1/aftermarket/listings/{domain}", json=payload) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data

        return await run_resilient("godaddy_auctions", update_listing, policy=RetryPolicy(attempts=2, timeout_seconds=20.0))

    def _headers(self) -> dict[str, str]:
        key = self.settings.godaddy_api_key.get_secret_value() if self.settings.godaddy_api_key else ""
        secret = self.settings.godaddy_api_secret.get_secret_value() if self.settings.godaddy_api_secret else ""
        return {"Authorization": f"sso-key {key}:{secret}", "Content-Type": "application/json"}
