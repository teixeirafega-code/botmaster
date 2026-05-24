from __future__ import annotations

from typing import Any

import aiohttp

from app.config.settings import Settings
from app.utils.retry import async_retry


class NamecheapRegistrar:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @async_retry(attempts=3)
    async def check_available(self, domain: str) -> bool:
        if self.settings.paper_mode:
            return True
        required = [
            self.settings.namecheap_api_user,
            self.settings.namecheap_api_key,
            self.settings.namecheap_username,
            self.settings.namecheap_client_ip,
        ]
        if not all(required):
            raise RuntimeError("Namecheap credentials are required when PAPER_MODE=false")
        assert self.settings.namecheap_api_key is not None
        params: dict[str, Any] = {
            "ApiUser": self.settings.namecheap_api_user,
            "ApiKey": self.settings.namecheap_api_key.get_secret_value(),
            "UserName": self.settings.namecheap_username,
            "ClientIp": self.settings.namecheap_client_ip,
            "Command": "namecheap.domains.check",
            "DomainList": domain,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(self.settings.namecheap_base_url, params=params) as resp:
                resp.raise_for_status()
                body = await resp.text()
                return 'Available="true"' in body
