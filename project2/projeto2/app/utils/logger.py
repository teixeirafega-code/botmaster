from __future__ import annotations

from pathlib import Path

from app.observability.logging import setup_json_logging


def setup_logging(log_file: Path, level: int = 20) -> None:
    setup_json_logging(log_file, level)
