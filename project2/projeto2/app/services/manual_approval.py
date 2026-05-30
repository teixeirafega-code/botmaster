from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.economics.models import ValuationResult
from app.models import DomainCandidate


class ManualApprovalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def is_approved(self, domain: str) -> bool:
        entry = self.approval_entry(domain)
        return bool(isinstance(entry, dict) and entry.get("approved") is True)

    def approved_by(self, domain: str) -> str:
        entry = self.approval_entry(domain)
        if not isinstance(entry, dict):
            return "manual_approval_file"
        approved_by = entry.get("approved_by") or entry.get("reviewed_by") or "manual_approval_file"
        return str(approved_by)

    def approval_entry(self, domain: str) -> dict[str, Any] | None:
        entry = self._read().get(domain.lower())
        return entry if isinstance(entry, dict) else None

    def upsert_pending(self, candidate: DomainCandidate, valuation: ValuationResult, reason: str, price: float) -> None:
        payload = self._read()
        key = candidate.name.lower()
        existing = payload.get(key) if isinstance(payload.get(key), dict) else {}
        approved = bool(existing.get("approved")) if isinstance(existing, dict) else False
        payload[key] = {
            "domain": candidate.name,
            "approved": approved,
            "reason": reason,
            "score": candidate.score,
            "price": round(price, 2),
            "liquidity_grade": valuation.liquidity_grade,
            "sale_probability": valuation.sale_probability,
            "expected_value": valuation.expected_value,
            "trademark_risk": valuation.trademark_risk,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if isinstance(existing, dict) and existing.get("created_at"):
            payload[key]["created_at"] = existing["created_at"]
        else:
            payload[key]["created_at"] = datetime.now(UTC).isoformat()
        self._write(payload)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
