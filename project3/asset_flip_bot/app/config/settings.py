from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip('"').strip("'")


def _minimal_yaml_load(path: Path) -> dict[str, Any]:
    """Small YAML subset loader used when PyYAML is not installed."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except ImportError:
        return _minimal_yaml_load(path)


def _deep_get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return float(value)


@dataclass(slots=True)
class MarketplaceSettings:
    name: str
    enabled: bool
    urls: list[str]
    cookie_env: str = ""
    timeout_seconds: int = 20
    min_delay_seconds: float = 1.5


@dataclass(slots=True)
class TelegramSettings:
    enabled: bool
    bot_token: str
    chat_id: str
    timeout_seconds: int = 12


@dataclass(slots=True)
class AppSettings:
    paper_mode: bool
    scan_interval_minutes: int
    log_level: str
    log_dir: Path
    state_path: Path
    stats_path: Path
    max_listing_age_days: int
    min_score_alert: int
    undervalued_threshold: float
    marketplaces: list[MarketplaceSettings] = field(default_factory=list)
    multipliers: dict[str, tuple[float, float]] = field(default_factory=dict)
    niche_bonus: dict[str, int] = field(default_factory=dict)
    telegram: TelegramSettings | None = None


def load_settings(
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> AppSettings:
    root = PROJECT_ROOT
    config_path = Path(config_path or root / "config.yaml")
    env_path = Path(env_path or root / ".env")
    _load_dotenv(env_path)
    data = load_yaml_config(config_path)

    app_data = _deep_get(data, "app", {})
    scoring_data = _deep_get(data, "scoring", {})
    valuation_data = _deep_get(data, "valuation", {})

    paper_mode = _env_bool("PAPER_MODE", bool(app_data.get("paper_mode", True)))
    interval = _env_int(
        "SCAN_INTERVAL_MINUTES",
        int(app_data.get("scan_interval_minutes", 30)),
    )
    log_level = os.getenv("LOG_LEVEL", str(app_data.get("log_level", "INFO")))

    def resolve_path(raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        return path

    marketplaces = []
    for name, marketplace_data in (_deep_get(data, "marketplaces", {}) or {}).items():
        enabled_key = f"{name.upper()}_ENABLED"
        cookie_env = str(marketplace_data.get("cookie_env", f"{name.upper()}_COOKIE"))
        marketplaces.append(
            MarketplaceSettings(
                name=name,
                enabled=_env_bool(enabled_key, bool(marketplace_data.get("enabled", True))),
                urls=list(marketplace_data.get("urls", [])),
                cookie_env=cookie_env,
                timeout_seconds=int(
                    _deep_get(marketplace_data, "request.timeout_seconds", 20)
                ),
                min_delay_seconds=float(
                    _deep_get(marketplace_data, "request.min_delay_seconds", 1.5)
                ),
            )
        )

    raw_multipliers = valuation_data.get("multipliers", {})
    multipliers: dict[str, tuple[float, float]] = {}
    for key, value in raw_multipliers.items():
        if isinstance(value, list | tuple) and len(value) == 2:
            multipliers[str(key)] = (float(value[0]), float(value[1]))

    telegram_data = _deep_get(data, "notifications.telegram", {})
    telegram = TelegramSettings(
        enabled=_env_bool("TELEGRAM_ENABLED", bool(telegram_data.get("enabled", False))),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", str(telegram_data.get("bot_token", ""))),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", str(telegram_data.get("chat_id", ""))),
        timeout_seconds=int(telegram_data.get("timeout_seconds", 12)),
    )

    return AppSettings(
        paper_mode=paper_mode,
        scan_interval_minutes=interval,
        log_level=log_level,
        log_dir=resolve_path(str(app_data.get("log_dir", "logs"))),
        state_path=resolve_path(str(app_data.get("state_path", "data/assets_state.json"))),
        stats_path=resolve_path(str(app_data.get("stats_path", "data/profit_stats.json"))),
        max_listing_age_days=int(app_data.get("max_listing_age_days", 14)),
        min_score_alert=_env_int("MIN_SCORE_ALERT", int(scoring_data.get("min_score_alert", 70))),
        undervalued_threshold=_env_float(
            "UNDERVALUED_THRESHOLD",
            float(scoring_data.get("undervalued_threshold", 0.5)),
        ),
        marketplaces=marketplaces,
        multipliers=multipliers,
        niche_bonus={str(k).lower(): int(v) for k, v in scoring_data.get("niche_bonus", {}).items()},
        telegram=telegram,
    )


