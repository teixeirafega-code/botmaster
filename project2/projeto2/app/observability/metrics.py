from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RuntimeStatus:
    scheduler_running: bool = False
    last_successful_scan: datetime | None = None
    queue_depth: int = 0
    domains_scanned: int = 0
    domains_registered: int = 0
    alerts_sent: int = 0
    critical_failures: int = 0
    api_retries: int = 0
    retry_budget_exhaustions: int = 0
    event_published: int = 0
    event_dead_letters: int = 0
    scheduler_skipped_overlaps: int = 0
    duplicate_registrations_prevented: int = 0
    slow_operations: int = 0
    provider_status: dict[str, str] = field(default_factory=dict)
    provider_failures: dict[str, int] = field(default_factory=dict)

    def mark_scan_success(self, count: int) -> None:
        self.last_successful_scan = datetime.now(UTC)
        self.domains_scanned += count


runtime_status = RuntimeStatus()


def prometheus_metrics() -> str:
    last_scan = runtime_status.last_successful_scan.timestamp() if runtime_status.last_successful_scan else 0
    lines = [
        "# HELP domain_hunter_domains_scanned_total Total scanned domains.",
        "# TYPE domain_hunter_domains_scanned_total counter",
        f"domain_hunter_domains_scanned_total {runtime_status.domains_scanned}",
        "# HELP domain_hunter_domains_registered_total Total registered domains.",
        "# TYPE domain_hunter_domains_registered_total counter",
        f"domain_hunter_domains_registered_total {runtime_status.domains_registered}",
        "# HELP domain_hunter_alerts_sent_total Total Telegram alerts sent.",
        "# TYPE domain_hunter_alerts_sent_total counter",
        f"domain_hunter_alerts_sent_total {runtime_status.alerts_sent}",
        "# HELP domain_hunter_last_successful_scan_timestamp Last successful scan unix timestamp.",
        "# TYPE domain_hunter_last_successful_scan_timestamp gauge",
        f"domain_hunter_last_successful_scan_timestamp {last_scan}",
        "# HELP domain_hunter_scheduler_running Scheduler state.",
        "# TYPE domain_hunter_scheduler_running gauge",
        f"domain_hunter_scheduler_running {1 if runtime_status.scheduler_running else 0}",
        "# HELP domain_hunter_api_retries_total Total API retries.",
        "# TYPE domain_hunter_api_retries_total counter",
        f"domain_hunter_api_retries_total {runtime_status.api_retries}",
        "# HELP domain_hunter_event_dead_letters_total Total dead-lettered internal events.",
        "# TYPE domain_hunter_event_dead_letters_total counter",
        f"domain_hunter_event_dead_letters_total {runtime_status.event_dead_letters}",
        "# HELP domain_hunter_duplicate_registrations_prevented_total Duplicate financial operations blocked.",
        "# TYPE domain_hunter_duplicate_registrations_prevented_total counter",
        f"domain_hunter_duplicate_registrations_prevented_total {runtime_status.duplicate_registrations_prevented}",
        "# HELP domain_hunter_async_tasks Current asyncio task count.",
        "# TYPE domain_hunter_async_tasks gauge",
        f"domain_hunter_async_tasks {__import__('asyncio').all_tasks().__len__()}",
    ]
    return "\n".join(lines) + "\n"
