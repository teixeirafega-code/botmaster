from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from web3 import HTTPProvider, Web3
from web3.middleware import ExtraDataToPOAMiddleware


@dataclass(frozen=True)
class Web3Client:
    chain_name: str
    rpc_url: str
    timeout_seconds: int = 10

    def create(self) -> Web3:
        provider = HTTPProvider(self.rpc_url, request_kwargs={"timeout": self.timeout_seconds})
        w3 = Web3(provider)
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return w3

    @classmethod
    def create_from_any(cls, chain_name: str, rpc_urls: list[str], timeout_seconds: int = 10) -> Web3:
        last_exc: Optional[Exception] = None
        for rpc_url in rpc_urls:
            try:
                client = cls(chain_name=chain_name, rpc_url=rpc_url, timeout_seconds=timeout_seconds)
                w3 = client.create()
                w3.eth.get_block_number()
                return w3
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        assert last_exc is not None
        raise last_exc


async def pick_working_rpc(rpc_urls: list[str], timeout_seconds: int = 10) -> str:
    last_exc: Optional[Exception] = None
    for url in rpc_urls:
        try:
            client = Web3Client(chain_name="", rpc_url=url, timeout_seconds=timeout_seconds)
            w3 = client.create()
            await asyncio.to_thread(w3.eth.get_block_number)
            return url
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    assert last_exc is not None
    raise last_exc
