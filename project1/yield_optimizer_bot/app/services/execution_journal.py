from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class JournalEntry:
    operation_id: str
    idempotency_key: str
    operation_type: str
    status: str
    created_at: float
    updated_at: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    tx_hashes: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    nonce: int | None = None
    error: str | None = None


class ExecutionJournal:
    def __init__(self, journal_path: str):
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries = self._load()

    def _load(self) -> dict[str, JournalEntry]:
        if not self.journal_path.exists():
            return {}
        raw = json.loads(self.journal_path.read_text(encoding="utf-8"))
        entries: dict[str, JournalEntry] = {}
        for operation_id, payload in raw.items():
            entries[operation_id] = JournalEntry(**payload)
        return entries

    def _persist(self) -> None:
        payload = {operation_id: asdict(entry) for operation_id, entry in self._entries.items()}
        self.journal_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def start_operation(self, operation_type: str, idempotency_key: str, metadata: dict[str, Any] | None = None) -> JournalEntry:
        existing = self.find_by_idempotency_key(idempotency_key)
        if existing and existing.status not in {"failed", "completed"}:
            return existing

        now = time.time()
        entry = JournalEntry(
            operation_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            operation_type=operation_type,
            status="started",
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        self._entries[entry.operation_id] = entry
        self._persist()
        return entry

    def find_by_idempotency_key(self, idempotency_key: str) -> JournalEntry | None:
        for entry in self._entries.values():
            if entry.idempotency_key == idempotency_key:
                return entry
        return None

    def append_step(self, operation_id: str, step: str, status: str, payload: dict[str, Any] | None = None) -> JournalEntry:
        entry = self._entries[operation_id]
        entry.steps.append(
            {
                "ts": time.time(),
                "step": step,
                "status": status,
                "payload": payload or {},
            }
        )
        entry.status = status
        entry.updated_at = time.time()
        self._persist()
        return entry

    def record_broadcast(self, operation_id: str, tx_hash: str, nonce: int | None, receipt: dict[str, Any] | None = None) -> JournalEntry:
        entry = self._entries[operation_id]
        entry.tx_hashes.append(tx_hash)
        if receipt is not None:
            entry.receipts.append(receipt)
        entry.nonce = nonce
        entry.updated_at = time.time()
        self._persist()
        return entry

    def mark_completed(self, operation_id: str, payload: dict[str, Any] | None = None) -> JournalEntry:
        return self.append_step(operation_id, step="completed", status="completed", payload=payload)

    def mark_failed(self, operation_id: str, error: str, payload: dict[str, Any] | None = None) -> JournalEntry:
        entry = self._entries[operation_id]
        entry.error = error
        return self.append_step(operation_id, step="failed", status="failed", payload=payload)

    def pending_operations(self) -> list[JournalEntry]:
        return [entry for entry in self._entries.values() if entry.status not in {"completed", "failed"}]
