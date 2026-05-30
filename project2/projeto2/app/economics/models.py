from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class ComparableSale:
    domain: str
    sale_price: float
    sold_at: datetime
    marketplace: str = "unknown"
    niche: str = "general"
    extension: str = ".com"


@dataclass(frozen=True)
class ValuationFactors:
    comparable_sales: float
    commercial_intent: float
    cpc_value: float
    search_demand: float
    extension_quality: float
    linguistic_quality: float
    brandability: float
    length_quality: float
    pronounceability: float
    trend_momentum: float
    seo_authority: float
    backlink_quality: float
    spam_safety: float
    trademark_safety: float
    archive_quality: float
    liquidity_probability: float

    def as_dict(self) -> dict[str, float]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ValuationResult:
    domain: str
    score: int
    fair_market_value: float
    expected_resale_probability: float
    estimated_holding_days: int
    expected_sale_price: float
    expected_roi: float
    liquidity_adjusted_roi: float
    time_adjusted_roi: float
    purchase_confidence: float
    recommended_purchase_price: float
    recommended_list_price: int
    niche: str
    extension: str
    factors: ValuationFactors
    comparable_count: int = 0
    market_signals: dict[str, float | int | str] = field(default_factory=dict)
    estimated_sale_price: float = 0.0
    sale_probability: float = 0.0
    expected_holding_months: float = 0.0
    expected_value: float = 0.0
    liquidity_grade: str = "D"
    trademark_risk: bool = False
    trademark_reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class AcquisitionDecision:
    approved: bool
    reason: str
    valuation: ValuationResult
