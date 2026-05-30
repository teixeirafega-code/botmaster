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
            proxy_url = self.settings.backlink_proxy_url.format(domain=domain)
            async with session.get(proxy_url, timeout=timeout) as resp:
                if resp.status < 400:
                    content_type = resp.headers.get("Content-Type", "").lower()
                    if "json" in content_type:
                        payload = await resp.json()
                        return self._count_backlink_payload(payload)
                    text = await resp.text()
                    return len([line for line in text.splitlines() if line.strip()])
        return 0

    def _count_backlink_payload(self, payload: Any) -> int:
        if isinstance(payload, dict):
            for key in ("backlinks", "backlink_count", "total", "count", "referring_domains"):
                if key in payload:
                    return int(float(payload[key] or 0))
            return max((self._count_backlink_payload(value) for value in payload.values()), default=0)
        if isinstance(payload, list):
            return len(payload)
        return 0
