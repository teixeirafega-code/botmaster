from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev


@dataclass(frozen=True)
class BacktestTrade:
    domain: str
    acquisition_cost: float
    expected_sale_price: float
    actual_sale_price: float
    hold_days: int
    approved: bool


@dataclass(frozen=True)
class BacktestReport:
    trades: int
    hit_rate: float
    roi: float
    average_hold_days: float
    sharpe_like: float
    false_positive_rate: float
    capital_utilization: float


class BacktestingEngine:
    def run(self, trades: list[BacktestTrade]) -> BacktestReport:
        approved = [trade for trade in trades if trade.approved]
        if not approved:
            return BacktestReport(0, 0, 0, 0, 0, 0, 0)
        returns = [(trade.actual_sale_price - trade.acquisition_cost) / max(trade.acquisition_cost, 1) for trade in approved]
        hits = [trade for trade in approved if trade.actual_sale_price > trade.acquisition_cost]
        false_positives = [trade for trade in approved if trade.actual_sale_price <= trade.acquisition_cost]
        avg_return = mean(returns)
        volatility = pstdev(returns) if len(returns) > 1 else 0
        return BacktestReport(
            trades=len(approved),
            hit_rate=len(hits) / len(approved),
            roi=sum(trade.actual_sale_price - trade.acquisition_cost for trade in approved) / sum(trade.acquisition_cost for trade in approved),
            average_hold_days=mean(trade.hold_days for trade in approved),
            sharpe_like=avg_return / volatility if volatility else avg_return,
            false_positive_rate=len(false_positives) / len(approved),
            capital_utilization=sum(trade.acquisition_cost for trade in approved),
        )

