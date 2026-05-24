from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.blockchain.gas_estimator import GasEstimator


def _make_w3_with_eip1559(base_fee_wei: int = 30_000_000_000) -> MagicMock:
    w3 = MagicMock()
    w3.eth.block_number = 100
    w3.eth.gas_price = 31_000_000_000
    w3.eth.get_block.side_effect = lambda block_id: {
        "baseFeePerGas": base_fee_wei if block_id == "latest" else base_fee_wei + ((100 - int(block_id)) * 1_000_000_000),
    }
    w3.to_wei.side_effect = lambda value, unit: int(float(value) * 1_000_000_000) if unit == "gwei" else None
    return w3


def test_estimate_uses_eip1559_and_clamps_max_fee():
    w3 = _make_w3_with_eip1559(base_fee_wei=40_000_000_000)
    estimator = GasEstimator(w3)

    estimate = estimator.estimate(priority_fee_gwei=2, max_gwei=70, max_gas_volatility_bps=300)

    assert estimate.max_priority_fee_per_gas_wei == 2_000_000_000
    assert estimate.max_fee_per_gas_wei == 70_000_000_000
    assert estimate.gas_price_wei is None
    assert 0.0 <= estimate.congestion_level <= 1.0


@pytest.mark.asyncio
async def test_estimate_transaction_cost_usd_uses_chainlink_price_feed():
    w3 = _make_w3_with_eip1559(base_fee_wei=30_000_000_000)

    latest_round_data_call = MagicMock(return_value=(1, 125_000_000, 0, 0, 1))
    decimals_call = MagicMock(return_value=8)
    contract = SimpleNamespace(
        functions=SimpleNamespace(
            latestRoundData=lambda: SimpleNamespace(call=latest_round_data_call),
            decimals=lambda: SimpleNamespace(call=decimals_call),
        )
    )
    w3.eth.contract.return_value = contract

    estimator = GasEstimator(w3)
    gas_estimate = estimator.estimate(priority_fee_gwei=2, max_gwei=100, max_gas_volatility_bps=300)

    tx_cost_usd = await estimator.estimate_transaction_cost_usd(300_000, gas_estimate)

    expected_gas_price = gas_estimate.max_fee_per_gas_wei
    expected_native = (expected_gas_price * 300_000) / 10**18
    assert tx_cost_usd == pytest.approx(expected_native * 1.25)
