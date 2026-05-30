from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class PurchaseAttemptStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def record(
        self,
        *,
        domain: str,
        price: float,
        registrar: str,
        approved_by: str,
        blocked_by_dry_run: bool,
        policy_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        attempts = self._read()
        attempt = {
            "domain": domain,
            "price": round(price, 2),
            "registrar": registrar,
            "approved_by": approved_by,
            "timestamp": datetime.now(UTC).isoformat(),
            "blocked_by_dry_run": blocked_by_dry_run,
            "policy_snapshot": policy_snapshot,
        }
        attempts.append(attempt)
        self._write(attempts)
        return attempt

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _write(self, attempts: list[dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(attempts, indent=2, sort_keys=True), encoding="utf-8")
