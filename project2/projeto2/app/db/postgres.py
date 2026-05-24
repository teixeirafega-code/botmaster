from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.models import DomainStatus, ManagedDomain

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned_domains (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,
    source TEXT NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    UNIQUE(domain, source)
);
CREATE TABLE IF NOT EXISTS scored_domains (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,
    score INT NOT NULL,
    age_years INT NOT NULL,
    backlinks INT NOT NULL,
    google_indexed BOOLEAN NOT NULL,
    keyword_value INT NOT NULL,
    extension_points INT NOT NULL,
    accepted BOOLEAN NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scanned_domains_domain ON scanned_domains(domain);
CREATE INDEX IF NOT EXISTS idx_scored_domains_domain_scored_at ON scored_domains(domain, scored_at DESC);
CREATE TABLE IF NOT EXISTS registrations (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    registrar TEXT NOT NULL,
    score INT NOT NULL,
    cost NUMERIC(12, 2) NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registrations_status ON registrations(status);
CREATE TABLE IF NOT EXISTS listings (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    price NUMERIC(12, 2) NOT NULL,
    status TEXT NOT NULL,
    listed_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    UNIQUE(domain, marketplace)
);
CREATE INDEX IF NOT EXISTS idx_listings_domain ON listings(domain);
CREATE TABLE IF NOT EXISTS sales (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,
    marketplace TEXT,
    sale_price NUMERIC(12, 2) NOT NULL,
    sold_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT,
    reason TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    scanned_count INT NOT NULL DEFAULT 0,
    error TEXT,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_history (
    id BIGSERIAL PRIMARY KEY,
    alert_type TEXT NOT NULL,
    message TEXT NOT NULL,
    delivered BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_valuations (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,
    score INT NOT NULL,
    fair_market_value NUMERIC(12, 2) NOT NULL,
    expected_resale_probability NUMERIC(8, 4) NOT NULL,
    estimated_holding_days INT NOT NULL,
    expected_roi NUMERIC(12, 4) NOT NULL,
    time_adjusted_roi NUMERIC(12, 4) NOT NULL,
    purchase_confidence NUMERIC(8, 4) NOT NULL,
    recommended_list_price NUMERIC(12, 2) NOT NULL,
    niche TEXT NOT NULL,
    extension TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_domain_valuations_domain_created ON domain_valuations(domain, created_at DESC);
CREATE TABLE IF NOT EXISTS comparable_sales (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,
    sale_price NUMERIC(12, 2) NOT NULL,
    marketplace TEXT NOT NULL,
    niche TEXT NOT NULL,
    extension TEXT NOT NULL,
    sold_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comparable_sales_niche_extension ON comparable_sales(niche, extension);
CREATE TABLE IF NOT EXISTS strategy_backtests (
    id BIGSERIAL PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    roi NUMERIC(12, 4) NOT NULL,
    hit_rate NUMERIC(8, 4) NOT NULL,
    average_hold_days NUMERIC(10, 2) NOT NULL,
    false_positive_rate NUMERIC(8, 4) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'registrations_status_allowed'
    ) THEN
        ALTER TABLE registrations ADD CONSTRAINT registrations_status_allowed
        CHECK (status IN ('pending', 'registered', 'listed', 'sold', 'failed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'registrations_score_range'
    ) THEN
        ALTER TABLE registrations ADD CONSTRAINT registrations_score_range CHECK (score BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'scored_domains_score_range'
    ) THEN
        ALTER TABLE scored_domains ADD CONSTRAINT scored_domains_score_range CHECK (score BETWEEN 0 AND 100);
    END IF;
END $$;
"""


class DomainRepository:
    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def healthcheck(self) -> bool:
        raise NotImplementedError

    async def init_schema(self) -> None:
        raise NotImplementedError

    async def scanned_exists(self, domain: str) -> bool:
        raise NotImplementedError

    async def save_scanned(self, domain: str, source: str, discovered_at: datetime, correlation_id: str, operation_id: str) -> None:
        raise NotImplementedError

    async def save_scored(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def registration_exists(self, domain: str) -> bool:
        raise NotImplementedError

    async def try_reserve_registration(
        self,
        domain: str,
        registrar: str,
        score: int,
        idempotency_key: str,
        correlation_id: str,
        operation_id: str,
    ) -> bool:
        raise NotImplementedError

    async def save_registration(self, domain: ManagedDomain, idempotency_key: str, correlation_id: str, operation_id: str) -> None:
        raise NotImplementedError

    async def mark_registration_failed(self, domain: str, reason: str, correlation_id: str, operation_id: str) -> None:
        raise NotImplementedError

    async def save_listing(self, domain: str, marketplace: str, price: int, correlation_id: str, operation_id: str) -> None:
        raise NotImplementedError

    async def save_risk_event(self, domain: str | None, reason: str, severity: str, correlation_id: str, operation_id: str) -> None:
        raise NotImplementedError

    async def save_alert(self, alert_type: str, message: str, delivered: bool, correlation_id: str, operation_id: str) -> None:
        raise NotImplementedError

    async def save_valuation(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def list_managed_domains(self) -> list[ManagedDomain]:
        raise NotImplementedError

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class PostgresDomainRepository(DomainRepository):
    def __init__(self, database_url: str, min_size: int = 1, max_size: int = 5) -> None:
        self.database_url = database_url
        self.min_size = min_size
        self.max_size = max_size
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=self.min_size,
            max_size=self.max_size,
            command_timeout=30,
            server_settings={"statement_timeout": "30000", "idle_in_transaction_session_timeout": "30000"},
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def healthcheck(self) -> bool:
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                return bool(await conn.fetchval("SELECT 1"))
        except Exception:
            logger.exception("db_healthcheck_failed", extra={"event_name": "db_healthcheck_failed"})
            return False

    async def init_schema(self) -> None:
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if not self.pool:
            yield
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield

    async def scanned_exists(self, domain: str) -> bool:
        assert self.pool
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1 FROM scanned_domains WHERE domain=$1", domain))

    async def save_scanned(self, domain: str, source: str, discovered_at: datetime, correlation_id: str, operation_id: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scanned_domains(domain, source, discovered_at, correlation_id, operation_id)
                VALUES($1, $2, $3, $4, $5)
                ON CONFLICT(domain, source) DO NOTHING
                """,
                domain,
                source,
                discovered_at,
                correlation_id,
                operation_id,
            )

    async def save_scored(self, payload: dict[str, Any]) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scored_domains(
                    domain, score, age_years, backlinks, google_indexed, keyword_value,
                    extension_points, accepted, scored_at, correlation_id, operation_id
                )
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                payload["domain"],
                payload["score"],
                payload["age_years"],
                payload["backlinks"],
                payload["google_indexed"],
                payload["keyword_value"],
                payload["extension_points"],
                payload["accepted"],
                datetime.now(UTC),
                payload["correlation_id"],
                payload["operation_id"],
            )

    async def registration_exists(self, domain: str) -> bool:
        assert self.pool
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1 FROM registrations WHERE domain=$1", domain))

    async def try_reserve_registration(
        self,
        domain: str,
        registrar: str,
        score: int,
        idempotency_key: str,
        correlation_id: str,
        operation_id: str,
    ) -> bool:
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO registrations(
                    domain, registrar, score, cost, idempotency_key, status, registered_at, correlation_id, operation_id
                )
                VALUES($1,$2,$3,0,$4,'pending',$5,$6,$7)
                ON CONFLICT(domain) DO NOTHING
                RETURNING id
                """,
                domain,
                registrar,
                score,
                idempotency_key,
                datetime.now(UTC),
                correlation_id,
                operation_id,
            )
            return row is not None

    async def save_registration(self, domain: ManagedDomain, idempotency_key: str, correlation_id: str, operation_id: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO registrations(domain, registrar, score, cost, idempotency_key, status, registered_at, correlation_id, operation_id)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT(domain) DO UPDATE SET
                    score=EXCLUDED.score,
                    cost=EXCLUDED.cost,
                    status=EXCLUDED.status,
                    registered_at=EXCLUDED.registered_at,
                    correlation_id=EXCLUDED.correlation_id,
                    operation_id=EXCLUDED.operation_id
                """,
                domain.name,
                domain.registrar or "godaddy",
                domain.score,
                domain.acquisition_cost,
                idempotency_key,
                domain.status.value,
                domain.registered_at or datetime.now(UTC),
                correlation_id,
                operation_id,
            )

    async def mark_registration_failed(self, domain: str, reason: str, correlation_id: str, operation_id: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE registrations
                    SET status='failed', correlation_id=$2, operation_id=$3
                    WHERE domain=$1 AND status='pending'
                    """,
                    domain,
                    correlation_id,
                    operation_id,
                )
                await conn.execute(
                    """
                    INSERT INTO risk_events(domain, reason, severity, created_at, correlation_id, operation_id)
                    VALUES($1,$2,'error',$3,$4,$5)
                    """,
                    domain,
                    reason,
                    datetime.now(UTC),
                    correlation_id,
                    operation_id,
                )

    async def save_listing(self, domain: str, marketplace: str, price: int, correlation_id: str, operation_id: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO listings(domain, marketplace, price, status, listed_at, correlation_id, operation_id)
                VALUES($1,$2,$3,'created',$4,$5,$6)
                ON CONFLICT(domain, marketplace) DO NOTHING
                """,
                domain,
                marketplace,
                price,
                datetime.now(UTC),
                correlation_id,
                operation_id,
            )

    async def save_risk_event(self, domain: str | None, reason: str, severity: str, correlation_id: str, operation_id: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO risk_events(domain, reason, severity, created_at, correlation_id, operation_id) VALUES($1,$2,$3,$4,$5,$6)",
                domain,
                reason,
                severity,
                datetime.now(UTC),
                correlation_id,
                operation_id,
            )

    async def save_alert(self, alert_type: str, message: str, delivered: bool, correlation_id: str, operation_id: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO alert_history(alert_type, message, delivered, created_at, correlation_id, operation_id) VALUES($1,$2,$3,$4,$5,$6)",
                alert_type,
                message,
                delivered,
                datetime.now(UTC),
                correlation_id,
                operation_id,
            )

    async def save_valuation(self, payload: dict[str, Any]) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO domain_valuations(
                    domain, score, fair_market_value, expected_resale_probability, estimated_holding_days,
                    expected_roi, time_adjusted_roi, purchase_confidence, recommended_list_price,
                    niche, extension, created_at, correlation_id, operation_id
                )
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                payload["domain"],
                payload["score"],
                payload["fair_market_value"],
                payload["expected_resale_probability"],
                payload["estimated_holding_days"],
                payload["expected_roi"],
                payload["time_adjusted_roi"],
                payload["purchase_confidence"],
                payload["recommended_list_price"],
                payload["niche"],
                payload["extension"],
                datetime.now(UTC),
                payload["correlation_id"],
                payload["operation_id"],
            )

    async def list_managed_domains(self) -> list[ManagedDomain]:
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT domain, registrar, score, cost, status, registered_at FROM registrations ORDER BY registered_at DESC")
        return [
            ManagedDomain(
                name=row["domain"],
                source="postgres",
                status=DomainStatus(row["status"]),
                score=row["score"],
                acquisition_cost=float(row["cost"]),
                registrar=row["registrar"],
                registered_at=row["registered_at"],
            )
            for row in rows
        ]


class MemoryDomainRepository(DomainRepository):
    def __init__(self) -> None:
        self.scanned: set[str] = set()
        self.domains: dict[str, ManagedDomain] = {}
        self.alerts: list[tuple[str, str, bool]] = []
        self.risk_events: list[tuple[str | None, str, str]] = []
        self.valuations: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def healthcheck(self) -> bool:
        return True

    async def init_schema(self) -> None:
        return None

    async def scanned_exists(self, domain: str) -> bool:
        return domain in self.scanned

    async def save_scanned(self, domain: str, source: str, discovered_at: datetime, correlation_id: str, operation_id: str) -> None:
        async with self._lock:
            self.scanned.add(domain)

    async def save_scored(self, payload: dict[str, Any]) -> None:
        return None

    async def registration_exists(self, domain: str) -> bool:
        async with self._lock:
            return domain in self.domains

    async def try_reserve_registration(
        self,
        domain: str,
        registrar: str,
        score: int,
        idempotency_key: str,
        correlation_id: str,
        operation_id: str,
    ) -> bool:
        async with self._lock:
            if domain in self.domains:
                return False
            self.domains[domain] = ManagedDomain(
                name=domain,
                source="memory",
                status=DomainStatus.REGISTERED,
                score=score,
                registrar=registrar,
            )
            return True

    async def save_registration(self, domain: ManagedDomain, idempotency_key: str, correlation_id: str, operation_id: str) -> None:
        async with self._lock:
            self.domains[domain.name] = domain

    async def mark_registration_failed(self, domain: str, reason: str, correlation_id: str, operation_id: str) -> None:
        async with self._lock:
            existing = self.domains.get(domain)
            if existing:
                existing.status = DomainStatus.FAILED
            self.risk_events.append((domain, reason, "error"))

    async def save_listing(self, domain: str, marketplace: str, price: int, correlation_id: str, operation_id: str) -> None:
        return None

    async def save_risk_event(self, domain: str | None, reason: str, severity: str, correlation_id: str, operation_id: str) -> None:
        async with self._lock:
            self.risk_events.append((domain, reason, severity))

    async def save_alert(self, alert_type: str, message: str, delivered: bool, correlation_id: str, operation_id: str) -> None:
        async with self._lock:
            self.alerts.append((alert_type, message, delivered))

    async def save_valuation(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self.valuations.append(payload)

    async def list_managed_domains(self) -> list[ManagedDomain]:
        async with self._lock:
            return list(self.domains.values())


def build_repository(database_url: str | None) -> DomainRepository:
    if database_url:
        return PostgresDomainRepository(database_url)
    return MemoryDomainRepository()
