from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev


@dataclass(frozen=True)
class BacktestTrade:
    domain: str
    acquisition_cost: float
    expected_sale_price: float
    actual_sale_price: float
    hold_days: int
    approved: bool
    transaction_cost: float = 0.0
    quantity: float = 1.0
    point_value: float = 1.0
    period: str = "all"


@dataclass(frozen=True)
class BacktestReport:
    trades: int
    hit_rate: float
    roi: float
    average_hold_days: float
    sharpe_like: float
    false_positive_rate: float
    capital_utilization: float


@dataclass(frozen=True)
class CostScenario:
    name: str
    cost_multiplier: float = 1.0
    slippage_points: float = 0.0
    spread_points: float = 0.0
    modest: bool = False


@dataclass(frozen=True)
class StressScenarioResult:
    scenario: str
    net_pnl: float
    sharpe: float
    drawdown: float
    profit_factor: float
    expectancy: float
    failed_periods: int
    failed_period_names: tuple[str, ...]
    profitable: bool


@dataclass(frozen=True)
class StressTestReport:
    scenarios: tuple[StressScenarioResult, ...]
    profitable_scenarios: tuple[str, ...]
    fragile: bool


class BacktestingEngine:
    DEFAULT_COST_SCENARIOS: tuple[CostScenario, ...] = (
        CostScenario("base"),
        CostScenario("2x costs", cost_multiplier=2.0, modest=True),
        CostScenario("3x costs", cost_multiplier=3.0),
        CostScenario("slippage 10 points", slippage_points=10.0, modest=True),
        CostScenario("slippage 15 points", slippage_points=15.0),
        CostScenario("spread 10 points", spread_points=10.0, modest=True),
        CostScenario("spread 15 points", spread_points=15.0),
    )

    def run(self, trades: list[BacktestTrade]) -> BacktestReport:
        approved = [trade for trade in trades if trade.approved]
        if not approved:
            return BacktestReport(0, 0, 0, 0, 0, 0, 0)
        returns = [self._base_pnl(trade) / max(trade.acquisition_cost, 1) for trade in approved]
        hits = [trade for trade in approved if self._base_pnl(trade) > 0]
        false_positives = [trade for trade in approved if self._base_pnl(trade) <= 0]
        avg_return = mean(returns)
        volatility = pstdev(returns) if len(returns) > 1 else 0
        return BacktestReport(
            trades=len(approved),
            hit_rate=len(hits) / len(approved),
            roi=sum(self._base_pnl(trade) for trade in approved) / sum(trade.acquisition_cost for trade in approved),
            average_hold_days=mean(trade.hold_days for trade in approved),
            sharpe_like=avg_return / volatility if volatility else avg_return,
            false_positive_rate=len(false_positives) / len(approved),
            capital_utilization=sum(trade.acquisition_cost for trade in approved),
        )

    def run_stress_test(
        self,
        trades: list[BacktestTrade],
        output_dir: str | Path = ".",
        scenarios: tuple[CostScenario, ...] | None = None,
    ) -> StressTestReport:
        approved = [trade for trade in trades if trade.approved]
        scenario_set = scenarios or self.DEFAULT_COST_SCENARIOS
        results = tuple(self._run_cost_scenario(approved, scenario) for scenario in scenario_set)
        profitable_scenarios = tuple(result.scenario for result in results if result.profitable)
        base_result = next((result for result in results if result.scenario == "base"), None)
        modest_scenarios = {scenario.name for scenario in scenario_set if scenario.modest}
        fragile = bool(base_result and base_result.profitable and any(not result.profitable for result in results if result.scenario in modest_scenarios))
        report = StressTestReport(scenarios=results, profitable_scenarios=profitable_scenarios, fragile=fragile)
        self._write_stress_outputs(report, Path(output_dir))
        return report

    def _run_cost_scenario(self, trades: list[BacktestTrade], scenario: CostScenario) -> StressScenarioResult:
        trade_pnls = [self._scenario_pnl(trade, scenario) for trade in trades]
        period_pnls: dict[str, float] = {}
        for trade, pnl in zip(trades, trade_pnls, strict=True):
            period_pnls[trade.period or "all"] = period_pnls.get(trade.period or "all", 0.0) + pnl

        failed_period_names = tuple(period for period, pnl in period_pnls.items() if pnl <= 0)
        net_pnl = sum(trade_pnls)
        gross_profit = sum(pnl for pnl in trade_pnls if pnl > 0)
        gross_loss = abs(sum(pnl for pnl in trade_pnls if pnl < 0))
        avg_pnl = mean(trade_pnls) if trade_pnls else 0.0
        volatility = pstdev(trade_pnls) if len(trade_pnls) > 1 else 0.0
        return StressScenarioResult(
            scenario=scenario.name,
            net_pnl=net_pnl,
            sharpe=avg_pnl / volatility if volatility else avg_pnl,
            drawdown=self._max_drawdown(trade_pnls),
            profit_factor=self._profit_factor(gross_profit, gross_loss),
            expectancy=avg_pnl,
            failed_periods=len(failed_period_names),
            failed_period_names=failed_period_names,
            profitable=net_pnl > 0,
        )

    def _scenario_pnl(self, trade: BacktestTrade, scenario: CostScenario) -> float:
        point_cost = (scenario.slippage_points + scenario.spread_points) * trade.point_value * trade.quantity
        return self._gross_pnl(trade) - (trade.transaction_cost * scenario.cost_multiplier) - point_cost

    def _base_pnl(self, trade: BacktestTrade) -> float:
        return self._scenario_pnl(trade, self.DEFAULT_COST_SCENARIOS[0])

    @staticmethod
    def _gross_pnl(trade: BacktestTrade) -> float:
        return trade.actual_sale_price - trade.acquisition_cost

    @staticmethod
    def _profit_factor(gross_profit: float, gross_loss: float) -> float:
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @staticmethod
    def _max_drawdown(pnls: list[float]) -> float:
        peak = 0.0
        cumulative = 0.0
        max_drawdown = 0.0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        return max_drawdown

    def _write_stress_outputs(self, report: StressTestReport, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "stress_test_report.txt").write_text(self._format_stress_report(report), encoding="utf-8")
        with (output_dir / "stress_test_scenarios.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "scenario",
                    "profitable",
                    "net_pnl",
                    "sharpe",
                    "drawdown",
                    "profit_factor",
                    "expectancy",
                    "failed_periods",
                    "failed_period_names",
                ],
            )
            writer.writeheader()
            for result in report.scenarios:
                writer.writerow(
                    {
                        "scenario": result.scenario,
                        "profitable": result.profitable,
                        "net_pnl": self._format_number(result.net_pnl),
                        "sharpe": self._format_number(result.sharpe),
                        "drawdown": self._format_number(result.drawdown),
                        "profit_factor": self._format_number(result.profit_factor),
                        "expectancy": self._format_number(result.expectancy),
                        "failed_periods": result.failed_periods,
                        "failed_period_names": "|".join(result.failed_period_names),
                    }
                )

    def _format_stress_report(self, report: StressTestReport) -> str:
        rows = [
            "Stress test report",
            "==================",
            f"Profitable scenarios: {', '.join(report.profitable_scenarios) if report.profitable_scenarios else 'none'}",
            f"Strategy fragility: {'FRAGILE' if report.fragile else 'ROBUST'}",
            "",
            "scenario | profitable | net_pnl | sharpe | drawdown | profit_factor | expectancy | failed_periods",
            "-" * 105,
        ]
        for result in report.scenarios:
            rows.append(
                " | ".join(
                    [
                        result.scenario,
                        "yes" if result.profitable else "no",
                        self._format_number(result.net_pnl),
                        self._format_number(result.sharpe),
                        self._format_number(result.drawdown),
                        self._format_number(result.profit_factor),
                        self._format_number(result.expectancy),
                        f"{result.failed_periods} ({', '.join(result.failed_period_names) if result.failed_period_names else 'none'})",
                    ]
                )
            )
        return "\n".join(rows) + "\n"

    @staticmethod
    def _format_number(value: float) -> str:
        if value == float("inf"):
            return "inf"
        return f"{value:.6f}"
