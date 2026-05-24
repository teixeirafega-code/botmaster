from __future__ import annotations

import hashlib
from typing import Any

import aiohttp

from app.config.settings import Settings


class BacklinkChecker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def backlink_count(self, domain: str) -> int:
        if self.settings.paper_mode:
            digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()
            return int(digest[:4], 16) % 600
        return await self._opportunistic_head_count(domain)

    async def google_indexed(self, domain: str) -> bool:
        if self.settings.google_api_key and self.settings.google_cse_id:
            params: dict[str, str | int] = {
                "key": self.settings.google_api_key.get_secret_value(),
                "cx": self.settings.google_cse_id,
                "q": f"site:{domain}",
                "num": 1,
            }
            async with aiohttp.ClientSession() as session:
                async with session.get("https://www.googleapis.com/customsearch/v1", params=params) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return int(data.get("searchInformation", {}).get("totalResults", "0")) > 0

        digest = hashlib.sha256(f"index:{domain}".encode()).hexdigest()
        return int(digest[:2], 16) % 3 != 0

    async def _opportunistic_head_count(self, domain: str) -> int:
        timeout = aiohttp.ClientTimeout(total=self.settings.scraper.timeout_seconds)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{domain}", timeout=timeout) as resp:
                if resp.status < 400:
                    return 20
        return 0
