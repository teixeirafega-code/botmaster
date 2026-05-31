from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

FinalDecision = Literal["rejected", "watchlist", "pending_approval", "dry_run_purchase", "purchased"]

AUDIT_DOMAINS = (
    "trainedrunner.com",
    "shbaihe.net",
    "ncshyundai.com",
    "jainmetals.net",
    "offkai-event.com",
)


@dataclass(frozen=True)
class CanonicalAcquisitionDecision:
    domain: str
    score: int
    estimated_sale_price: float
    price: float
    extension: str
    trademark_risk: bool
    trademark_reason: str
    liquidity_grade: str
    sale_probability: float
    expected_holding_months: float
    expected_value: float
    passed_score_filter: bool
    passed_trademark_filter: bool
    passed_liquidity_filter: bool
    passed_extension_filter: bool
    passed_price_filter: bool
    passed_budget_filter: bool
    final_decision: FinalDecision
    final_reason: str
    timestamp: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["domain"] = self.domain.lower()
        return payload


class AcquisitionDecisionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, decision: CanonicalAcquisitionDecision) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision.as_dict(), sort_keys=True, ensure_ascii=True) + "\n")

    def list_decisions(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        decisions: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("domain"):
                payload["domain"] = str(payload["domain"]).lower()
                decisions.append(payload)
        return decisions

    def latest_by_domain(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for decision in self.list_decisions():
            domain = str(decision.get("domain") or "").lower()
            if not domain:
                continue
            previous = latest.get(domain)
            if not previous or str(decision.get("timestamp") or "") >= str(previous.get("timestamp") or ""):
                latest[domain] = decision
        return latest

    def sync_pending_approvals(self, pending_path: Path) -> None:
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._read_pending(pending_path)
        latest = self.latest_by_domain()
        pending: dict[str, dict[str, Any]] = {}
        for domain, decision in latest.items():
            if decision.get("final_decision") != "pending_approval":
                continue
            existing = previous.get(domain) if isinstance(previous.get(domain), dict) else {}
            created_at = existing.get("created_at") if isinstance(existing, dict) else None
            pending[domain] = {
                "domain": domain,
                "approved": bool(existing.get("approved")) if isinstance(existing, dict) else False,
                "reason": decision.get("final_reason") or "manual_approval_required",
                "score": decision.get("score"),
                "price": decision.get("price"),
                "liquidity_grade": decision.get("liquidity_grade"),
                "sale_probability": decision.get("sale_probability"),
                "expected_value": decision.get("expected_value"),
                "trademark_risk": decision.get("trademark_risk") is True,
                "created_at": created_at or decision.get("timestamp") or datetime.now(UTC).isoformat(),
                "updated_at": decision.get("timestamp") or datetime.now(UTC).isoformat(),
            }
            if isinstance(existing, dict):
                for key in ("approved_by", "reviewed_by", "reviewer_notes", "final_decision"):
                    if key in existing:
                        pending[domain][key] = existing[key]
        pending_path.write_text(json.dumps(pending, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8")

    @staticmethod
    def _read_pending(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key).lower(): value for key, value in payload.items()}
