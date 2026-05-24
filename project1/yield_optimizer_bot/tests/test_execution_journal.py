from __future__ import annotations

from app.services.execution_journal import ExecutionJournal


def test_execution_journal_persists_receipts_and_recovers(tmp_path):
    journal_path = tmp_path / "journal.json"
    journal = ExecutionJournal(str(journal_path))
    entry = journal.start_operation("rebalance", "idem-1", {"asset_symbol": "USDC"})
    journal.record_broadcast(entry.operation_id, "0xabc", 9, {"status": 1, "simulated": True})

    recovered = ExecutionJournal(str(journal_path))
    same = recovered.find_by_idempotency_key("idem-1")

    assert same is not None
    assert same.tx_hashes == ["0xabc"]
    assert same.receipts[0]["simulated"] is True
