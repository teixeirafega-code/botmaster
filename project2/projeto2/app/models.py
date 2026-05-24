from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DomainStatus(StrEnum):
    MONITORED = "monitored"
    REGISTERED = "registered"
    LISTED = "listed"
    SOLD = "sold"
    FAILED = "failed"


class DomainCandidate(BaseModel):
    name: str
    source: str
    age_years: int = 0
    backlinks: int = 0
    google_indexed: bool = False
    keyword_value: int = 0
    extension_points: int = 0
    score: int = 0
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ManagedDomain(BaseModel):
    name: str
    source: str
    status: DomainStatus
    score: int
    asking_price: int = 0
    acquisition_cost: float = 0.0
    sale_price: float = 0.0
    registrar: str | None = None
    marketplaces: list[str] = Field(default_factory=list)
    registered_at: datetime | None = None
    sold_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

