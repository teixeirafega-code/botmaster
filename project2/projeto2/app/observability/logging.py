from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger

from app.core.context import correlation_id_var, execution_mode_var, operation_id_var

SECRET_PATTERN = re.compile(r"(?i)(token|secret|api[_-]?key|authorization|password)=?['\"]?([^,'\"\s]+)")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.service = "domain_hunter_bot"
        record.correlation_id = correlation_id_var.get() or "-"
        record.operation_id = operation_id_var.get() or "-"
        record.execution_mode = execution_mode_var.get() or "paper"
        record.event_name = getattr(record, "event_name", record.getMessage())
        record.severity = record.levelname.lower()
        for attr in ("domain", "score"):
            if not hasattr(record, attr):
                setattr(record, attr, None)
        return True


class RedactingJsonFormatter(jsonlogger.JsonFormatter):
    def process_log_record(self, log_record: dict[str, Any]) -> dict[str, Any]:
        for key, value in list(log_record.items()):
            if isinstance(value, str):
                log_record[key] = SECRET_PATTERN.sub(r"\1=[redacted]", value)
            if any(secret_key in key.lower() for secret_key in ("token", "secret", "password", "authorization")):
                log_record[key] = "[redacted]"
        return log_record


def setup_json_logging(log_file: Path, level: int = logging.INFO) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = (
        "%(asctime)s %(service)s %(event_name)s %(severity)s %(levelname)s %(name)s "
        "%(message)s %(correlation_id)s %(execution_mode)s %(domain)s %(score)s %(operation_id)s"
    )
    formatter = RedactingJsonFormatter(fmt=fmt, rename_fields={"asctime": "timestamp", "levelname": "level"})
    context_filter = ContextFilter()

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(context_filter)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)
