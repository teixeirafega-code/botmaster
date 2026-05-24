from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass
from typing import Any, Optional

from web3 import Web3

from app.utils.retry import RetryConfig, retry_async


@dataclass(frozen=True)
class GasEstimate:
    max_fee_per_gas_wei: int
    max_priority_fee_per_gas_wei: int
    gas_price_wei: Optional[int]
    congestion_level: float  # 0..1


class GasEstimator:
    _MATIC_USD_FEED = Web3.to_checksum_address("0xAB594600376Ec9fD91F8e885dADF0CE036862dE0")
    _CHAINLINK_AGGREGATOR_ABI: list[dict[str, Any]] = [
        {
            "inputs": [],
            "name": "decimals",
            "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [],
            "name": "latestRoundData",
            "outputs": [
                {"internalType": "uint80", "name": "roundId", "type": "uint80"},
                {"internalType": "int256", "name": "answer", "type": "int256"},
                {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
                {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
                {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    def __init__(self, w3: Web3):
        self.w3 = w3
        self.logger = logging.getLogger("yield-optimizer-bot.blockchain.gas_estimator")
        self.price_feed = self.w3.eth.contract(address=self._MATIC_USD_FEED, abi=self._CHAINLINK_AGGREGATOR_ABI)

    def _sample_base_fee(self, sample_count: int = 5) -> list[int]:
        base_fees: list[int] = []
        latest = self.w3.eth.block_number
        for i in range(sample_count):
            b = self.w3.eth.get_block(latest - i)
            bf = b.get("baseFeePerGas")
            if bf is not None:
                base_fees.append(int(bf))
        base_fees.reverse()
        return base_fees

    def estimate(self, priority_fee_gwei: float, max_gwei: float, max_gas_volatility_bps: int) -> GasEstimate:
        latest_block = self.w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")

        if base_fee is None:
            gas_price = int(self.w3.eth.gas_price)
            return GasEstimate(
                max_fee_per_gas_wei=gas_price,
                max_priority_fee_per_gas_wei=0,
                gas_price_wei=gas_price,
                congestion_level=0.5,
            )

        base_fee = int(base_fee)
        prio_fee_wei = self.w3.to_wei(priority_fee_gwei, "gwei")
        max_fee_per_gas = int(2 * base_fee + prio_fee_wei)

        samples = self._sample_base_fee(sample_count=6)
        congestion_level = 0.0
        if len(samples) >= 2:
            stdev = statistics.stdev(samples)
            mean = statistics.mean(samples)
            if mean > 0:
                congestion_level = min(1.0, (stdev / mean) * 5)

        max_gas_price_wei = self.w3.to_wei(max_gwei, "gwei")
        if max_fee_per_gas > max_gas_price_wei:
            max_fee_per_gas = max_gas_price_wei

        return GasEstimate(
            max_fee_per_gas_wei=max_fee_per_gas,
            max_priority_fee_per_gas_wei=prio_fee_wei,
            gas_price_wei=None,
            congestion_level=congestion_level,
        )

    async def get_native_token_price_usd(self) -> float:
        async def fetch_price() -> float:
            round_data = await asyncio.to_thread(self.price_feed.functions.latestRoundData().call)
            decimals = await asyncio.to_thread(self.price_feed.functions.decimals().call)
            answer = int(round_data[1])
            if answer <= 0:
                raise RuntimeError("Chainlink MATIC/USD returned a non-positive price")
            return answer / (10 ** int(decimals))

        return await retry_async(
            fetch_price,
            config=RetryConfig(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=8.0),
            logger=self.logger,
        )

    async def estimate_transaction_cost_usd(self, gas_limit: int, gas_estimate: GasEstimate) -> float:
        if gas_limit <= 0:
            raise ValueError("gas_limit must be > 0")

        gas_price_wei = gas_estimate.gas_price_wei or gas_estimate.max_fee_per_gas_wei
        matic_usd = await self.get_native_token_price_usd()
        tx_cost_native = (gas_price_wei * int(gas_limit)) / 10**18
        tx_cost_usd = tx_cost_native * matic_usd

        self.logger.info(
            "Gas USD estimated | gas_limit=%s gas_price_wei=%s matic_usd=%.8f tx_cost_usd=%.8f",
            gas_limit,
            gas_price_wei,
            matic_usd,
            tx_cost_usd,
        )
        return tx_cost_usd
