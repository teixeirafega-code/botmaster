from __future__ import annotations

import pytest

from app.services.paper_portfolio import PaperPortfolioLedger
from app.services.paper_reconciliation import PaperReconciliationService


@pytest.mark.asyncio
async def test_paper_reconciliation_uses_virtual_balances(tmp_path):
    ledger = PaperPortfolioLedger(str(tmp_path / "paper_state.json"), initial_holdings={"USDC": 1_000_000})
    ledger.apply_simulated_rebalance(
        asset_symbol="USDC",
        amount_wei=400_000,
        withdraw_protocol=None,
        deposit_protocol="aave",
        expected_shares_wei=400_000,
        gas_cost_usd=1,
        slippage_cost_usd=1,
        apy=0.07,
    )
    service = PaperReconciliationService(ledger=ledger, wallet_address="0x1111111111111111111111111111111111111111", chain="Polygon")

    result = await service.reconcile(["USDC"])

    assert result.snapshot.totals_by_asset["USDC"] == 1_000_000
    assert result.snapshot.dominant_protocol_by_asset["USDC"] == "aave"
