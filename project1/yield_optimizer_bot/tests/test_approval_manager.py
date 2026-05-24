from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from web3 import Web3

from app.blockchain.approval_manager import ApprovalManager, ApprovalPolicy


@pytest.mark.asyncio
async def test_check_allowance_requires_dynamic_refresh():
    owner = "0x1111111111111111111111111111111111111111"
    spender = "0x2222222222222222222222222222222222222222"
    token_address = "0x3333333333333333333333333333333333333333"

    token_contract = SimpleNamespace(
        functions=SimpleNamespace(
            allowance=lambda _owner, _spender: SimpleNamespace(call=MagicMock(return_value=50)),
            balanceOf=lambda _owner: SimpleNamespace(call=MagicMock(return_value=500)),
        )
    )
    w3 = MagicMock()
    w3.eth.contract.return_value = token_contract

    manager = ApprovalManager(
        w3=w3,
        owner_address=owner,
        whitelisted_spenders={spender},
        policy=ApprovalPolicy(mode="dynamic", exact_allowance=False, min_refresh_ratio=0.95),
    )
    result = await manager.check_allowance(token_address=token_address, spender=spender, required_allowance_wei=100)

    assert result.approval_required is True
    assert result.approval_amount_wei == 100


def test_build_revoke_tx_sets_zero_allowance():
    owner = "0x1111111111111111111111111111111111111111"
    spender = "0x2222222222222222222222222222222222222222"
    token_address = "0x3333333333333333333333333333333333333333"
    manager = ApprovalManager(
        w3=Web3(),
        owner_address=owner,
        whitelisted_spenders={spender},
    )

    tx = manager.build_revoke_tx(token_address=token_address, spender=spender)

    assert tx["to"] == Web3.to_checksum_address(token_address)
    assert tx["data"].startswith("0x")
