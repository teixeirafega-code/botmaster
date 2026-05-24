from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.protocols.base_protocol import BaseProtocol, YieldQuote
from app.utils.retry import RetryConfig, retry_async


@dataclass(frozen=True)
class APYAggConfig:
    cache_ttl_seconds: int = 20
    timeout_seconds: int = 10


class APYAggAggregator:
    def __init__(self, protocols: list[BaseProtocol], cfg: APYAggConfig):
        self.protocols = protocols
        self.cfg = cfg
        self.logger = logging.getLogger("yield-optimizer-bot.services.apy_aggregator")
        # cache key: (protocol, asset)
        self._cache: Dict[Tuple[str, str], tuple[YieldQuote, float]] = {}

    def _get_cached(self, protocol: str, asset_symbol: str) -> Optional[YieldQuote]:
        key = (protocol, asset_symbol)
        if key not in self._cache:
            return None
        quote, expires_at = self._cache[key]
        if time.time() >= expires_at:
            return None
        return quote

    async def _fetch_one(self, protocol: BaseProtocol, asset_symbol: str) -> YieldQuote:
        cached = self._get_cached(protocol.protocol_name, asset_symbol)
        if cached is not None:
            return cached

        async def fn():
            return await protocol.fetch_apy(asset_symbol)

        quote = await retry_async(fn, RetryConfig(max_attempts=4), logger=None)
        self._cache[(protocol.protocol_name, asset_symbol)] = (quote, time.time() + self.cfg.cache_ttl_seconds)
        return quote

    async def aggregate(self, asset_symbol: str) -> List[YieldQuote]:
        results: list[YieldQuote] = []
        for p in self.protocols:
            try:
                results.append(await self._fetch_one(p, asset_symbol))
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Skipping protocol quote after fetch failure | protocol=%s asset=%s error=%s",
                    p.protocol_name,
                    asset_symbol,
                    exc,
                )
        if not results:
            raise RuntimeError(f"No protocol quotes available for asset {asset_symbol}")
        return results

