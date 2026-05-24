from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from app.botmaster_status import write_status
from app.blockchain.approval_manager import ApprovalManager, ApprovalPolicy
from app.blockchain.gas_estimator import GasEstimator
from app.blockchain.transaction_manager import ExecutionMode, TransactionManager
from app.blockchain.wallet import Wallet
from app.blockchain.web3_client import Web3Client
from app.config.settings import Settings
from app.protocols.aave import AaveProtocol
from app.protocols.base_protocol import BaseProtocol
from app.protocols.beefy import BeefyProtocol
from app.protocols.compound import CompoundProtocol
from app.protocols.curve import CurveProtocol
from app.protocols.stargate import StargateProtocol
from app.scheduler import SchedulerFactory
from app.services.apy_aggregator import APYAggAggregator, APYAggConfig
from app.services.execution_journal import ExecutionJournal
from app.services.paper_portfolio import PaperPortfolioLedger
from app.services.paper_reconciliation import PaperReconciliationService
from app.services.portfolio_manager import PortfolioManager
from app.services.position_indexer import PositionIndexer
from app.services.profitability_engine import ProfitabilityEngine, ProfitabilityInputs
from app.services.rebalance_engine import RebalanceEngine, RebalancePlan
from app.services.reconciliation import ReconciliationService
from app.services.risk_manager import RiskManager
from app.strategies.yield_strategy import YieldStrategy
from app.utils.logger import setup_logger


BOT_ROOT = Path(__file__).resolve().parents[1]

class YieldOptimizerBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = setup_logger()
        self.log_prefix = "DRY_RUN" if settings.is_dry_run else "LIVE"

        self.cfg = settings.config

        self.state_path = BOT_ROOT / "app" / "data" / "state.json"
        self.portfolio_manager = PortfolioManager(state_path=str(self.state_path))
        self.paper_portfolio = (
            PaperPortfolioLedger(
                state_path=self.cfg.app.paper.state_file,
                initial_holdings=self.cfg.app.paper.initial_holdings,
            )
            if settings.is_paper_trading
            else None
        )
        journal_path = self.cfg.app.paper.execution_journal_file if settings.is_paper_trading else str(BOT_ROOT / "app" / "data" / "execution_journal.json")
        self.journal = ExecutionJournal(journal_path=journal_path)

        protocols_map: dict[str, type[BaseProtocol]] = {
            "aave": AaveProtocol,
            "compound": CompoundProtocol,
            "curve": CurveProtocol,
            "beefy": BeefyProtocol,
            "stargate": StargateProtocol,
        }

        self.network_key = "polygon" if "polygon" in self.cfg.networks else next(iter(self.cfg.networks.keys()))
        self.chain_cfg = self.cfg.networks[self.network_key]

        # Wallet/tx manager
        self.wallet = Wallet.from_env(
            address=self.cfg.wallet.address,
            env_path=settings.env_path,
            require_private_key=not settings.is_dry_run,
        )
        self.w3 = Web3Client.create_from_any(
            chain_name=self.chain_cfg.name,
            rpc_urls=self.chain_cfg.rpc_urls,
        )

        self.protocols: list[BaseProtocol] = [
            protocols_map[name](
                chain=self.chain_cfg.name,
                w3=self.w3,
                wallet_address=self.wallet.address,
            )
            for name in self.cfg.protocols.enabled
        ]

        self.apy_aggregator = APYAggAggregator(
            protocols=self.protocols,
            cfg=APYAggConfig(
                cache_ttl_seconds=self.cfg.apy.cache_ttl_seconds,
                timeout_seconds=self.cfg.apy.timeout_seconds,
            ),
        )

        self.tx_manager = TransactionManager(
            w3=self.w3,
            account=self.wallet.account(),
            chain_id=self.chain_cfg.chain_id,
            execution_mode=ExecutionMode(
                dry_run=self.cfg.app.dry_run or not settings.is_production,
                execute_transactions=self.cfg.app.execute_transactions,
                sign_transactions=self.cfg.app.sign_transactions,
                paper_trading=self.cfg.app.paper_trading,
            ),
        )

        self.position_indexer = PositionIndexer(protocols=self.protocols, wallet_address=self.wallet.address)
        self.onchain_reconciliation_service = ReconciliationService(
            position_indexer=self.position_indexer,
            portfolio_manager=self.portfolio_manager,
        )
        self.reconciliation_service = (
            PaperReconciliationService(
                ledger=self.paper_portfolio,
                wallet_address=self.wallet.address,
                chain=self.chain_cfg.name,
            )
            if self.paper_portfolio is not None
            else self.onchain_reconciliation_service
        )
        whitelisted_spenders = {
            protocol.get_spender(asset_symbol)
            for protocol in self.protocols
            for asset_symbol in protocol.supported_assets()
        }
        self.approval_manager = ApprovalManager(
            w3=self.w3,
            owner_address=self.wallet.address,
            whitelisted_spenders=whitelisted_spenders,
            policy=ApprovalPolicy(mode="dynamic", exact_allowance=False),
        )
        self.profitability_engine = ProfitabilityEngine()

        self.rebalance_engine = RebalanceEngine(
            tx_manager=self.tx_manager,
            portfolio_manager=self.portfolio_manager,
            reconciliation_service=self.reconciliation_service,
            approval_manager=self.approval_manager,
            journal=self.journal,
            protocols={protocol.protocol_name: protocol for protocol in self.protocols},
            paper_portfolio=self.paper_portfolio,
        )

        self.risk_manager = RiskManager(
            emergency_stop_path=str(self.settings.emergency_stop_path),
            max_consecutive_failures=self.cfg.risk.max_consecutive_failures,
            rate_limit_per_minute=self.cfg.risk.rate_limit_per_minute,
        )

        exec_cfg = self.cfg.execution
        self.yield_strategy = YieldStrategy(
            min_apy_diff_bps=self.cfg.protocols.min_apy_diff_bps,
            cooldown_seconds=int(exec_cfg["cooldown_seconds"]),
            min_profit_usd=float(exec_cfg["min_profit_usd"]),
            slippage_bps=int(exec_cfg["slippage"]["bps"]),
        )
        for asset_symbol, observations in self.portfolio_manager.state.apy_history.items():
            self.yield_strategy.load_history(asset_symbol, observations)

        self.logger.info("%s | Bot initialized | network=%s chain_id=%s mode=%s dry_run=%s", self.log_prefix, self.network_key, self.chain_cfg.chain_id, self.cfg.app.mode, self.cfg.app.dry_run)

    def _status_metrics(self, asset_symbol: str | None = None) -> dict[str, object]:
        asset_symbol = asset_symbol or self.cfg.assets["stablecoins"][0]
        latest_apys: dict[str, float] = {}
        history = self.portfolio_manager.state.apy_history.get(asset_symbol, [])
        for item in history:
            protocol = str(item.get("protocol", "")).lower()
            if not protocol:
                continue
            try:
                latest_apys[protocol] = float(item.get("apy", 0.0))
            except (TypeError, ValueError):
                continue
        best_protocol = max(latest_apys.items(), key=lambda item: item[1], default=(None, None))[0]
        simulated_profit = None
        if self.paper_portfolio is not None:
            simulated_profit = self.paper_portfolio.state.analytics.hypothetical_pnl_usd
        return {
            "asset": asset_symbol,
            "apys": latest_apys,
            "best_protocol": best_protocol,
            "current_protocol": self.portfolio_manager.state.current_protocol,
            "simulated_profit": simulated_profit,
            "paper_mode": self.settings.is_paper_trading,
        }
    async def monitor_apy_job(self) -> None:
        asset_symbol = self.cfg.assets["stablecoins"][0]
        try:
            quotes = await self.apy_aggregator.aggregate(asset_symbol=asset_symbol)
            now_ts = time.time()
            observations = []
            quotes_sorted = sorted(quotes, key=lambda q: q.net_apy, reverse=True)
            ranking = []
            for q in quotes_sorted:
                self.yield_strategy.record_observation(q.protocol, asset_symbol, q.net_apy, now_ts)
                observations.append({"protocol": q.protocol, "asset_symbol": asset_symbol, "apy": q.net_apy, "ts": now_ts})
                ranking.append({"protocol": q.protocol, "net_apy": q.net_apy, "raw_apy": q.raw_apy})
                self.logger.info("%s | APY | asset=%s protocol=%s raw_apy=%s net_apy=%s", self.log_prefix, asset_symbol, q.protocol, q.raw_apy, q.net_apy)
            self.portfolio_manager.record_apy_observation(asset_symbol, observations)
            if self.paper_portfolio is not None:
                self.paper_portfolio.record_protocol_ranking(asset_symbol, ranking)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("%s | monitor_apy_job failed: %s", self.log_prefix, exc)

    async def rebalance_job(self) -> None:
        asset_symbol = self.cfg.assets["stablecoins"][0]
        exec_cfg = self.cfg.execution

        try:
            if self.paper_portfolio is not None:
                try:
                    onchain_reconciliation = await self.onchain_reconciliation_service.reconcile([asset_symbol])
                    self.logger.info(
                        "%s | Onchain reconciliation telemetry | inconsistencies=%s totals=%s",
                        self.log_prefix,
                        onchain_reconciliation.inconsistencies,
                        onchain_reconciliation.snapshot.totals_by_asset,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("%s | Onchain reconciliation telemetry failed: %s", self.log_prefix, exc)
            reconciliation = await self.reconciliation_service.reconcile([asset_symbol])
            snapshot = reconciliation.snapshot
            quotes = await self.apy_aggregator.aggregate(asset_symbol=asset_symbol)
            now_ts = time.time()
            observations = []
            for quote in quotes:
                self.yield_strategy.record_observation(quote.protocol, asset_symbol, quote.net_apy, now_ts)
                observations.append({"protocol": quote.protocol, "asset_symbol": asset_symbol, "apy": quote.net_apy, "ts": now_ts})
            self.portfolio_manager.record_apy_observation(asset_symbol, observations)

            best = max(quotes, key=lambda q: q.net_apy)
            current_protocol = snapshot.dominant_protocol_by_asset.get(asset_symbol) or self.portfolio_manager.state.current_protocol

            gas_estimator = GasEstimator(self.w3)
            gas_est = gas_estimator.estimate(
                priority_fee_gwei=float(exec_cfg["gas"]["priority_fee_gwei"]),
                max_gwei=float(exec_cfg["gas"]["max_gwei"]),
                max_gas_volatility_bps=int(exec_cfg["gas"]["max_gas_volatility_bps"]),
            )

            tx_count = 3 if current_protocol and current_protocol != best.protocol else 2
            estimated_gas_fee_usd = await gas_estimator.estimate_transaction_cost_usd(
                gas_limit=300_000 * tx_count,
                gas_estimate=gas_est,
            )

            current_net_apy = 0.0
            if current_protocol:
                for q in quotes:
                    if q.protocol == current_protocol:
                        current_net_apy = q.net_apy
                        break

            if current_protocol:
                current_position = snapshot.positions.get(current_protocol, {}).get(asset_symbol)
                amount_wei = int(current_position.supplied_balance_wei) if current_position else 0
            else:
                if self.paper_portfolio is not None:
                    amount_wei = int(self.paper_portfolio.get_wallet_balance(asset_symbol))
                else:
                    first_position = next(iter(snapshot.positions.values()), {})
                    amount_wei = int(first_position.get(asset_symbol).wallet_balance_wei) if first_position.get(asset_symbol) else 0

            if amount_wei <= 0:
                reason = "no_paper_balance" if self.paper_portfolio is not None else "no_onchain_balance"
                self.logger.info("%s | Rebalance skipped | asset=%s reason=%s", self.log_prefix, asset_symbol, reason)
                if self.paper_portfolio is not None:
                    self.paper_portfolio.record_rejection(reason, {"asset_symbol": asset_symbol})
                return

            capital_usd = amount_wei / 10**6
            best_protocol = next(p for p in self.protocols if p.protocol_name == best.protocol)
            best_health = await best_protocol.check_health(asset_symbol)
            liquidity_depth_score = min(1.0, best_health.liquidity_wei / max(amount_wei, 1))
            slippage_cost_usd = capital_usd * (int(exec_cfg["slippage"]["bps"]) / 10_000)
            profitability = self.profitability_engine.evaluate(
                ProfitabilityInputs(
                    capital_usd=capital_usd,
                    candidate_apy=best.net_apy,
                    current_apy=current_net_apy,
                    gas_cost_usd=estimated_gas_fee_usd,
                    slippage_cost_usd=slippage_cost_usd,
                    expected_holding_days=max(1.0, float(exec_cfg["cooldown_seconds"]) / 86_400),
                    protocol_risk_score=best_health.risk_score,
                    liquidity_depth_score=liquidity_depth_score,
                )
            )

            allow, reason = self.yield_strategy.should_rebalance(
                current_protocol=current_protocol,
                last_rebalance_ts=self.portfolio_manager.state.last_rebalance_ts,
                candidate_protocol=best.protocol,
                candidate_net_apy=best.net_apy,
                current_net_apy=current_net_apy,
                estimated_gas_fee_usd=estimated_gas_fee_usd,
                cooldown_now=now_ts,
                profitability=profitability,
                asset_symbol=asset_symbol,
            )

            risk_decision = self.risk_manager.decision(
                gas_congestion_level=gas_est.congestion_level,
                gas_volatility_bps=int(exec_cfg["gas"]["max_gas_volatility_bps"]),
                allow_operations=allow,
                protocol_paused=best_health.is_paused,
                oracle_sane=estimated_gas_fee_usd > 0,
                abnormal_apy=best.net_apy > 0.50,
                exposure_fraction=amount_wei / max(snapshot.totals_by_asset.get(asset_symbol, amount_wei), 1),
                max_protocol_exposure_fraction=1.0,
                confidence_score=min(best_health.confidence_score, profitability.confidence_score),
            )

            self.logger.info(
                "%s | Rebalance decision | best=%s current=%s allow=%s reason=%s risk=%s",
                self.log_prefix,
                best.protocol,
                current_protocol,
                allow,
                reason,
                risk_decision.reason,
            )
            self.logger.info(
                "%s | Profitability | asset=%s best=%s current=%s expected_profit_usd=%.6f payback_days=%.4f min_capital_usd=%.6f reasons=%s",
                self.log_prefix,
                asset_symbol,
                best.protocol,
                current_protocol,
                profitability.expected_profit_usd,
                profitability.payback_days,
                profitability.min_profitable_capital_usd,
                profitability.reasons,
            )

            if not risk_decision.allow:
                if self.paper_portfolio is not None:
                    self.paper_portfolio.record_rejection(
                        risk_decision.reason,
                        {
                            "asset_symbol": asset_symbol,
                            "best_protocol": best.protocol,
                            "current_protocol": current_protocol,
                            "expected_profit_usd": profitability.expected_profit_usd,
                        },
                    )
                self.logger.info(
                    "%s | Rebalance rejected | asset=%s best=%s current=%s strategy_reason=%s risk_reason=%s expected_profit_usd=%.6f",
                    self.log_prefix,
                    asset_symbol,
                    best.protocol,
                    current_protocol,
                    reason,
                    risk_decision.reason,
                    profitability.expected_profit_usd,
                )
                return

            plan = RebalancePlan(
                withdraw_protocol=current_protocol,
                deposit_protocol=best.protocol,
                asset_symbol=asset_symbol,
                amount_wei=amount_wei,
                profitability=profitability,
                metadata={
                    "strategy_reason": reason,
                    "gas_cost_usd": estimated_gas_fee_usd,
                    "slippage_cost_usd": slippage_cost_usd,
                    "current_net_apy": current_net_apy,
                    "candidate_net_apy": best.net_apy,
                },
            )

            outcome = await self.rebalance_engine.execute(plan=plan)
            self.logger.info(
                "%s | Simulated execution | operation_id=%s simulated=%s tx_hashes=%s",
                self.log_prefix,
                outcome.operation_id,
                outcome.simulated,
                outcome.tx_hashes,
            )
            self.risk_manager.record_success()

        except Exception as exc:  # noqa: BLE001
            self.risk_manager.record_failure()
            self.logger.exception("%s | rebalance_job failed: %s", self.log_prefix, exc)

    async def healthcheck_job(self) -> None:
        ok = True
        if self.settings.emergency_stop_path.exists():
            ok = False
        self.logger.info("%s | Healthcheck | ok=%s emergency_stop=%s", self.log_prefix, ok, self.settings.emergency_stop_path.exists())

    async def heartbeat_job(self) -> None:
        asset_symbol = self.cfg.assets["stablecoins"][0]
        if self.paper_portfolio is not None and self.cfg.app.paper.accrue_yield_on_heartbeat:
            latest_apys: dict[str, float] = {}
            history = self.portfolio_manager.state.apy_history.get(asset_symbol, [])
            for item in history:
                latest_apys[item["protocol"]] = float(item["apy"])
            accrued = self.paper_portfolio.apply_yield(asset_symbol, latest_apys)
            self.logger.info(
                "%s | Heartbeat | accrued_simulated_yield_usd=%.8f rebalance_count=%s pnl_usd=%.8f",
                self.log_prefix,
                accrued,
                self.paper_portfolio.state.analytics.rebalance_count,
                self.paper_portfolio.state.analytics.hypothetical_pnl_usd,
            )
            write_status("yield", "Yield Optimizer", "RUNNING", self._status_metrics(asset_symbol))
            return
        self.logger.info("%s | Heartbeat | ts=%s last_rebalance_ts=%s", self.log_prefix, int(time.time()), int(self.portfolio_manager.state.last_rebalance_ts))
        write_status("yield", "Yield Optimizer", "RUNNING", self._status_metrics(asset_symbol))

    async def run_forever(self) -> None:
        try:
            if self.paper_portfolio is not None:
                onchain_reconciliation = await self.onchain_reconciliation_service.reconcile(self.cfg.assets["stablecoins"])
                self.logger.info(
                    "%s | Startup onchain reconciliation | inconsistencies=%s current_protocol=%s",
                    self.log_prefix,
                    onchain_reconciliation.inconsistencies,
                    self.portfolio_manager.state.current_protocol,
                )
            reconciliation = await self.reconciliation_service.reconcile(self.cfg.assets["stablecoins"])
            self.logger.info(
                "%s | Startup reconciliation | inconsistencies=%s current_protocol=%s",
                self.log_prefix,
                reconciliation.inconsistencies,
                getattr(self.paper_portfolio.state if self.paper_portfolio is not None else self.portfolio_manager.state, "current_protocol", None),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("%s | startup reconciliation failed: %s", self.log_prefix, exc)

        if self.settings.is_dry_run and self.wallet.has_private_key:
            self.logger.warning("%s | Safety warning | private_key_present_in_dry_run=true", self.log_prefix)

        if self.settings.is_dry_run:
            try:
                funded_assets = []
                for asset_symbol in self.cfg.assets["stablecoins"]:
                    token_address = next(
                        protocol.get_asset_address(asset_symbol)
                        for protocol in self.protocols
                        if asset_symbol in protocol.supported_assets()
                    )
                    balance = await self.approval_manager.get_balance(token_address)
                    if balance > 0:
                        funded_assets.append({"asset_symbol": asset_symbol, "balance_wei": balance})
                if funded_assets:
                    self.logger.warning("%s | Safety warning | funded_wallet_connected=%s", self.log_prefix, funded_assets)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("%s | funded wallet check failed: %s", self.log_prefix, exc)

        pending_ops = self.journal.pending_operations()
        if pending_ops:
            self.logger.warning("%s | Pending operations detected on startup | count=%s", self.log_prefix, len(pending_ops))

        sched = SchedulerFactory.create(
            monitor_interval_seconds=self.cfg.scheduler.monitor_interval_seconds,
            rebalance_interval_seconds=self.cfg.scheduler.rebalance_interval_seconds,
            healthcheck_interval_seconds=self.cfg.scheduler.healthcheck_interval_seconds,
            heartbeat_interval_seconds=self.cfg.scheduler.heartbeat_interval_seconds,
            timezone=self.cfg.scheduler.timezone,
            monitor_job=self.monitor_apy_job,
            rebalance_job=self.rebalance_job,
            healthcheck_job=self.healthcheck_job,
            heartbeat_job=self.heartbeat_job,
        )

        sched.start()
        self.logger.info("%s | Scheduler started", self.log_prefix)
        write_status("yield", "Yield Optimizer", "RUNNING", self._status_metrics())

        # keep event loop alive
        while True:
            await asyncio.sleep(3600)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Yield Optimizer Bot")
    parser.add_argument("command", choices=["scheduler", "run"], nargs="?", default="scheduler")
    parser.parse_args(argv)

    settings = Settings.load(config_path=str(BOT_ROOT / "config.yaml"), env_path=str(BOT_ROOT / ".env"))
    bot = YieldOptimizerBot(settings=settings)

    try:
        asyncio.run(bot.run_forever())
    except Exception as exc:  # noqa: BLE001
        write_status("yield", "Yield Optimizer", "ERROR", bot._status_metrics(), str(exc))
        raise

if __name__ == "__main__":
    main()



