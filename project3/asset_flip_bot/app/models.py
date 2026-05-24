from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AssetType(str, Enum):
    WEBSITE = "website"
    APP = "app"
    YOUTUBE = "youtube"
    ECOMMERCE = "ecommerce"
    SAAS = "saas"
    NEWSLETTER = "newsletter"
    OTHER = "other"


@dataclass(slots=True)
class MarketplaceListing:
    marketplace: str
    external_id: str
    name: str
    url: str
    asset_type: AssetType = AssetType.OTHER
    asking_price: float = 0.0
    monthly_revenue: float = 0.0
    monthly_profit: float = 0.0
    age_months: int = 0
    monthly_traffic: int = 0
    niche: str = "unknown"
    currency: str = "USD"
    raw: dict[str, Any] = field(default_factory=dict)
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def stable_key(self) -> str:
        return f"{self.marketplace}:{self.external_id or self.url}".lower()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["asset_type"] = self.asset_type.value
        data["scraped_at"] = self.scraped_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketplaceListing":
        payload = dict(data)
        payload["asset_type"] = AssetType(payload.get("asset_type", AssetType.OTHER.value))
        scraped_at = payload.get("scraped_at")
        if isinstance(scraped_at, str):
            payload["scraped_at"] = datetime.fromisoformat(scraped_at)
        return cls(**payload)


@dataclass(slots=True)
class Valuation:
    low_value: float
    high_value: float
    estimated_real_value: float
    profit_potential: float
    discount_to_value: float
    multiplier_low: float
    multiplier_high: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class ScoredOpportunity:
    listing: MarketplaceListing
    valuation: Valuation
    opportunity_score: int
    is_undervalued: bool
    reasons: list[str] = field(default_factory=list)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "listing": self.listing.to_dict(),
            "valuation": self.valuation.to_dict(),
            "opportunity_score": self.opportunity_score,
            "is_undervalued": self.is_undervalued,
            "reasons": list(self.reasons),
            "detected_at": self.detected_at.isoformat(),
        }

