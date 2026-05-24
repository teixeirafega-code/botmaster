from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]

DEFAULT_BROWSER_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]


class ScraperSettings(BaseModel):
    whoisxml_url: str = "https://newly-registered-domains.whoisxmlapi.com/"
    whoisxml_download_urls: list[str] = Field(default_factory=list)
    whoisxml_download_limit: int = 3
    expireddomains_url: str = "https://www.expireddomains.net/deleted-domains/"
    expireddomains_max_pages: int = 3
    user_agent: str = "DomainHunterBot/1.0 (+https://localhost)"
    user_agents: list[str] = Field(default_factory=lambda: DEFAULT_BROWSER_USER_AGENTS.copy())
    timeout_seconds: int = 30


class ScoreSettings(BaseModel):
    registration_threshold: int = 60
    max_domains_per_cycle: int = 100
    extension_points: dict[str, int] = Field(
        default_factory=lambda: {".com": 10, ".net": 7, ".org": 5, ".io": 4}
    )


class PricingSettings(BaseModel):
    score_60_70: int = 200
    score_70_80: int = 500
    score_80_90: int = 1500
    score_90_100: int = 5000

    def price_for_score(self, score: int) -> int:
        if score >= 90:
            return self.score_90_100
        if score >= 80:
            return self.score_80_90
        if score >= 70:
            return self.score_70_80
        return self.score_60_70


class ValuationSettings(BaseModel):
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "comparable_sales": 0.12,
            "commercial_intent": 0.1,
            "cpc_value": 0.08,
            "search_demand": 0.08,
            "extension_quality": 0.09,
            "linguistic_quality": 0.08,
            "brandability": 0.08,
            "length_quality": 0.06,
            "pronounceability": 0.05,
            "trend_momentum": 0.07,
            "seo_authority": 0.05,
            "backlink_quality": 0.04,
            "spam_safety": 0.04,
            "trademark_safety": 0.04,
            "archive_quality": 0.01,
            "liquidity_probability": 0.11,
        }
    )


class EconomicsSettings(BaseModel):
    minimum_expected_roi: float = 2.0
    minimum_time_adjusted_roi: float = 0.25
    minimum_resale_probability: float = 0.18
    minimum_purchase_confidence: float = 0.45
    max_portfolio_capital: float = 1000.0
    max_extension_concentration: float = 0.65
    max_niche_concentration: float = 0.45


class SchedulerSettings(BaseModel):
    interval_minutes: int = 30
    timezone: str = "America/Sao_Paulo"
    cycle_timeout_seconds: int = 600
    max_instances: int = 1


class DatabaseSettings(BaseModel):
    url: SecretStr | None = None
    min_pool_size: int = 1
    max_pool_size: int = 5
    connect_timeout_seconds: int = 10


class RiskSettings(BaseModel):
    max_daily_registrations: int = 5
    max_capital_exposure: float = 250.0
    minimum_score: int = 60
    blacklist: list[str] = Field(default_factory=list)
    cooldown_minutes: int = 60
    emergency_stop: bool = False
    dry_run_audit: bool = False
    max_candidate_age_minutes: int = 120


class RuntimeSettings(BaseModel):
    max_concurrent_scoring: int = 10
    max_concurrent_registrations: int = 1
    event_handler_timeout_seconds: float = 10.0
    event_dead_letter_max: int = 500
    alert_cooldown_seconds: int = 300
    alert_rate_limit_per_minute: int = 10
    retry_budget_per_provider_per_minute: int = 20


class ObservabilitySettings(BaseModel):
    health_host: str = "0.0.0.0"
    health_port: int = 8080


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", env_nested_delimiter="__", extra="ignore")

    paper_mode: bool = True
    config_file: Path = BASE_DIR / "config.yaml"
    state_file: Path = BASE_DIR / "data" / "domains.json"
    log_file: Path = BASE_DIR / "logs" / "domain_hunter_bot.log"
    service_name: str = "domain_hunter_bot"

    godaddy_api_key: SecretStr | None = None
    godaddy_api_secret: SecretStr | None = None
    godaddy_base_url: str = "https://api.godaddy.com"
    namecheap_api_user: str | None = None
    namecheap_api_key: SecretStr | None = None
    namecheap_username: str | None = None
    namecheap_client_ip: str | None = None
    namecheap_base_url: str = "https://api.namecheap.com/xml.response"

    sedo_api_key: SecretStr | None = None
    sedo_base_url: str = "https://api.sedo.com/api/v1"
    afternic_api_key: SecretStr | None = None
    afternic_base_url: str = "https://api.afternic.com/v1"

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    google_api_key: SecretStr | None = None
    google_cse_id: str | None = None

    scraper: ScraperSettings = Field(default_factory=ScraperSettings)
    scoring: ScoreSettings = Field(default_factory=ScoreSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    valuation: ValuationSettings = Field(default_factory=ValuationSettings)
    economics: EconomicsSettings = Field(default_factory=EconomicsSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @model_validator(mode="after")
    def validate_live_mode(self) -> Settings:
        if not self.paper_mode and (not self.godaddy_api_key or not self.godaddy_api_secret):
            raise ValueError("GODADDY_API_KEY and GODADDY_API_SECRET are required when PAPER_MODE=false")
        return self


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache
def get_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    settings = Settings()
    if settings.config_file.exists():
        with settings.config_file.open("r", encoding="utf-8") as fh:
            yaml_data = yaml.safe_load(fh) or {}
        merged = _deep_merge(settings.model_dump(), yaml_data)
        settings = Settings.model_validate(merged)
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    return settings
