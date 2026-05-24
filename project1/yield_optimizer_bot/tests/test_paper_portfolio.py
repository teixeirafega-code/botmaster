from __future__ import annotations

from app.services.paper_portfolio import PaperPortfolioLedger


def test_paper_portfolio_persists_and_recovers_after_restart(tmp_path):
    state_path = tmp_path / "paper_state.json"
    ledger = PaperPortfolioLedger(str(state_path), initial_holdings={"USDC": 1_000_000})
    ledger.apply_simulated_rebalance(
        asset_symbol="USDC",
        amount_wei=500_000,
        withdraw_protocol=None,
        deposit_protocol="aave",
        expected_shares_wei=500_000,
        gas_cost_usd=2.5,
        slippage_cost_usd=1.0,
        apy=0.08,
    )

    recovered = PaperPortfolioLedger(str(state_path), initial_holdings={"USDC": 1_000_000})

    assert recovered.get_wallet_balance("USDC") == 500_000
    assert recovered.get_protocol_balance("aave", "USDC") == 500_000
    assert recovered.state.analytics.rebalance_count == 1


def test_paper_portfolio_accrues_yield(tmp_path):
    ledger = PaperPortfolioLedger(str(tmp_path / "paper_state.json"), initial_holdings={"USDC": 1_000_000})
    ledger.apply_simulated_rebalance(
        asset_symbol="USDC",
        amount_wei=1_000_000,
        withdraw_protocol=None,
        deposit_protocol="aave",
        expected_shares_wei=1_000_000,
        gas_cost_usd=0,
        slippage_cost_usd=0,
        apy=0.10,
    )

    before = ledger.get_protocol_balance("aave", "USDC")
    ledger.apply_yield("USDC", {"aave": 0.10}, now_ts=ledger.state.analytics.last_yield_accrual_ts + 86_400)
    after = ledger.get_protocol_balance("aave", "USDC")

    assert after > before
