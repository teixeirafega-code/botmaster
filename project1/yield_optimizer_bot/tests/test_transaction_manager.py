from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.blockchain.transaction_manager import BroadcastPolicy, ExecutionMode, TransactionManager, TxPlan


def _make_w3() -> MagicMock:
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.get_block.return_value = {"baseFeePerGas": 1}
    w3.eth.block_number = 123
    w3.eth.estimate_gas.return_value = 250_000
    w3.eth.call.return_value = b"\x12\x34"
    return w3


def test_execute_plan_dry_run_returns_simulated_transaction_and_never_broadcasts():
    w3 = _make_w3()
    account = SimpleNamespace(address="0x1111111111111111111111111111111111111111")

    manager = TransactionManager(
        w3=w3,
        account=account,
        chain_id=137,
        execution_mode=ExecutionMode(dry_run=True, execute_transactions=False, sign_transactions=False, paper_trading=True),
    )
    result = manager.execute_plan(
        TxPlan(
            tx={"to": "0x2222222222222222222222222222222222222222", "data": "0x1234", "maxFeePerGas": 1, "maxPriorityFeePerGas": 1},
            description="dry-run",
        ),
        BroadcastPolicy(),
    )

    assert result.simulated is True
    assert result.nonce == 7
    assert result.tx_hash.startswith("0x")
    assert result.receipt is not None
    w3.eth.send_raw_transaction.assert_not_called()


def test_sign_and_send_is_hard_blocked_in_dry_run():
    w3 = _make_w3()
    account = SimpleNamespace(address="0x1111111111111111111111111111111111111111")
    manager = TransactionManager(
        w3=w3,
        account=account,
        chain_id=137,
        execution_mode=ExecutionMode(dry_run=True, execute_transactions=False, sign_transactions=False, paper_trading=True),
    )

    with pytest.raises(RuntimeError, match="disabled"):
        manager.sign_and_send(
            TxPlan(
                tx={"to": "0x2222222222222222222222222222222222222222", "data": "0x1234", "maxFeePerGas": 1, "maxPriorityFeePerGas": 1},
                description="blocked",
            )
        )
