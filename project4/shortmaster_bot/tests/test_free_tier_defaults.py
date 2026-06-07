from __future__ import annotations

from pathlib import Path

from app.services.config import load_config
from app.services.trend_service import TrendService


def test_free_tier_defaults_do_not_require_paid_api(tmp_path: Path) -> None:
    root = tmp_path
    (root / "config.yaml").write_text(
        """
app:
  paper_mode: true
queue:
  manual_approval: true
trends:
  reddit:
    enabled: true
  google_trends:
    enabled: true
  tiktok:
    enabled: false
    provider: disabled
publishing:
  enable_real_upload: false
""",
        encoding="utf-8",
    )

    config = load_config(root)

    assert config["app"]["paper_mode"] is True
    assert config["queue"]["manual_approval"] is True
    assert config["publishing"]["live_upload_enabled"] is False
    assert config["publishing"]["enable_real_upload"] is False
    assert config["publishing"]["daily_upload_limit"] == 5
    assert config["publishing"]["max_daily_upload_limit"] == 5
    assert "api_key" not in config["generation"]["script"]


def test_tiktok_monitor_is_disabled_without_provider(tmp_path: Path) -> None:
    config = {
        "trends": {
            "reddit": {"enabled": False},
            "google_trends": {"enabled": False},
            "tiktok": {"enabled": True, "provider": "disabled"},
        }
    }

    service = TrendService(config)

    assert service.monitors == []
