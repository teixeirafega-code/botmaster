from __future__ import annotations

import textwrap

import pytest

from app.services.config import ConfigError, load_config


def write_config(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text(
        textwrap.dedent(
            """
            app:
              paper_mode: true
            publishing:
              enable_real_upload: false
              live_upload_enabled: false
              min_upload_quality_score: 75
              min_upload_safety_score: 90
            """
        ).strip(),
        encoding="utf-8",
    )


def test_paper_mode_false_is_blocked_outside_production(tmp_path, monkeypatch) -> None:
    write_config(tmp_path)
    monkeypatch.setenv("PAPER_MODE", "false")
    monkeypatch.setenv("SHORTSMASTER_ENV", "development")

    with pytest.raises(ConfigError, match="allowed only"):
        load_config(tmp_path)


def test_production_upload_thresholds_cannot_be_lowered(tmp_path, monkeypatch) -> None:
    write_config(tmp_path)
    monkeypatch.setenv("PAPER_MODE", "false")
    monkeypatch.setenv("SHORTSMASTER_ENV", "production")
    monkeypatch.setenv("ENABLE_REAL_UPLOAD", "true")
    monkeypatch.setenv("LIVE_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("MIN_UPLOAD_QUALITY_SCORE", "74")

    with pytest.raises(ConfigError, match="cannot be lower than 75"):
        load_config(tmp_path)
