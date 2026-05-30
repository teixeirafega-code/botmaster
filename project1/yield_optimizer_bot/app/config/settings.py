from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class NetworkConfig(BaseModel):
    chain_id: int
    name: str
    rpc_urls: list[str] = Field(min_length=1)


class ExecutionGasConfig(BaseModel):
    max_gas_volatility_bps: int = 300
    max_gwei: float = 250
    priority_fee_gwei: float = 1.5
    gas_limit_buffer: float = 1.15


class ExecutionSlippageConfig(BaseModel):
    bps: int = 20


class SchedulerConfig(BaseModel):
    timezone: str = "UTC"
    monitor_interval_seconds: int = 60
    rebalance_interval_seconds: int = 300
    healthcheck_interval_seconds: int = 120
    heartbeat_interval_seconds: int = 30


class RiskConfig(BaseModel):
    rate_limit_per_minute: int = 120
    max_consecutive_failures: int = 10


class APYConfig(BaseModel):
    cache_ttl_seconds: int = 20
    timeout_seconds: int = 10


class ProtocolsConfig(BaseModel):
    enabled: list[Literal["aave", "compound", "curve", "beefy", "stargate"]]
    min_apy_diff_bps: int = 75


class WalletConfig(BaseModel):
    address: str


class PaperTradingConfig(BaseModel):
    enabled: bool = True
    state_file: str = "app/data/paper_state.json"
    execution_journal_file: str = "app/data/paper_execution_journal.json"
    initial_holdings: dict[str, int] = Field(default_factory=lambda: {"USDC": 100_000 * 10**6})
    accrue_yield_on_heartbeat: bool = True


class AppConfig(BaseModel):
    mode: Literal["paper_trading", "production"] = "paper_trading"
    emergency_stop_file: str = "app/data/emergency_stop.flag"
    dry_run: bool = True
    execute_transactions: bool = False
    sign_transactions: bool = False
    paper_trading: bool = True
    paper: PaperTradingConfig = Field(default_factory=PaperTradingConfig)

    @model_validator(mode="after")
    def validate_safety(self) -> "AppConfig":
        if self.dry_run:
            if self.execute_transactions:
                raise ValueError("app.execute_transactions must be false when app.dry_run is true")
            if self.sign_transactions:
                raise ValueError("app.sign_transactions must be false when app.dry_run is true")
        if self.paper_trading and not self.dry_run:
            raise ValueError("app.paper_trading requires app.dry_run=true")
        if self.mode == "paper_trading" and not self.paper_trading:
            raise ValueError("app.mode=paper_trading requires app.paper_trading=true")
        return self


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: AppConfig
    networks: dict[str, NetworkConfig]
    wallet: WalletConfig
    assets: dict[str, list[str]]
    protocols: ProtocolsConfig
    execution: dict[str, Any]
    scheduler: SchedulerConfig
    risk: RiskConfig
    apy: APYConfig

    @field_validator("execution")
    @classmethod
    def validate_execution(cls, v: Any) -> Any:
        required = ["gas", "slippage", "cooldown_seconds", "min_profit_usd"]
        for k in required:
            if k not in v:
                raise ValueError(f"execution.{k} is required")
        return v


@dataclass(frozen=True)
class Settings:
    config: ConfigModel
    env_path: Path

    @classmethod
    def load(cls, config_path: str = "config.yaml", env_path: str = ".env") -> "Settings":
        resolved_config_path = Path(config_path)
        if not resolved_config_path.is_absolute():
            resolved_config_path = PROJECT_ROOT / resolved_config_path
        resolved_config_path = resolved_config_path.resolve()

        resolved_env_path = Path(env_path)
        if not resolved_env_path.is_absolute():
            resolved_env_path = (resolved_config_path.parent / resolved_env_path).resolve()

        env_values = dotenv_values(resolved_env_path) if resolved_env_path.exists() else {}
        for key, value in env_values.items():
            if value is None:
                continue
            current = os.environ.get(key)
            if current is None or not str(current).strip():
                os.environ[key] = str(value).strip()

        cfg = yaml.safe_load(resolved_config_path.read_text(encoding="utf-8"))
        cls._apply_rpc_env_overrides(cfg)
        model = ConfigModel.model_validate(cfg)
        return cls(config=model, env_path=resolved_env_path)

    @staticmethod
    def _apply_rpc_env_overrides(cfg: dict[str, Any]) -> None:
        networks = cfg.get("networks")
        if not isinstance(networks, dict):
            return
        for network_name, network_cfg in networks.items():
            if not isinstance(network_cfg, dict):
                continue
            env_names = (
                f"RPC_{str(network_name).upper()}_URL",
                f"RPC_{str(network_name).upper()}_URLS",
            )
            raw = next((os.environ.get(name) for name in env_names if os.environ.get(name)), None)
            if not raw:
                continue
            urls = [url.strip() for url in raw.split(",") if Settings._usable_rpc_url(url)]
            if urls:
                network_cfg["rpc_urls"] = urls

    @staticmethod
    def _usable_rpc_url(url: str) -> bool:
        normalized = url.strip()
        if not normalized:
            return False
        lowered = normalized.lower()
        return "your_rpc_endpoint" not in lowered and "example.invalid" not in lowered

    @property
    def is_production(self) -> bool:
        return self.config.app.mode == "production" and not self.config.app.dry_run

    @property
    def is_dry_run(self) -> bool:
        return bool(self.config.app.dry_run)

    @property
    def is_paper_trading(self) -> bool:
        return bool(self.config.app.paper_trading)

    @property
    def emergency_stop_path(self) -> Path:
        path = Path(self.config.app.emergency_stop_file)
        return path if path.is_absolute() else PROJECT_ROOT / path

