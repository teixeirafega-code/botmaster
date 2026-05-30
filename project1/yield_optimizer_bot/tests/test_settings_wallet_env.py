from __future__ import annotations

from pathlib import Path

from app.blockchain.wallet import Wallet
from app.config.settings import Settings


def test_settings_load_resolves_env_and_populates_missing_private_key(tmp_path: Path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PRIVATE_KEY='0xabc123'\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app:
  mode: paper_trading
  emergency_stop_file: "./stop.flag"
  dry_run: true
  execute_transactions: false
  sign_transactions: false
  paper_trading: true
  paper:
    enabled: true
    state_file: "./paper_state.json"
    execution_journal_file: "./journal.json"
    initial_holdings:
      USDC: 1000000
    accrue_yield_on_heartbeat: true
networks:
  polygon:
    chain_id: 137
    name: "Polygon"
    rpc_urls:
      - "https://example.invalid"
wallet:
  address: "0x000000000000000000000000000000000000dEaD"
assets:
  stablecoins:
    - USDC
protocols:
  enabled:
    - aave
execution:
  gas:
    max_gas_volatility_bps: 300
    max_gwei: 250
    priority_fee_gwei: 1.5
    gas_limit_buffer: 1.15
  slippage:
    bps: 20
  cooldown_seconds: 1800
  min_profit_usd: 3.0
scheduler:
  timezone: "UTC"
  monitor_interval_seconds: 60
  rebalance_interval_seconds: 300
  healthcheck_interval_seconds: 120
  heartbeat_interval_seconds: 30
risk:
  rate_limit_per_minute: 120
  max_consecutive_failures: 10
apy:
  cache_ttl_seconds: 20
  timeout_seconds: 10
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.setenv("RPC_POLYGON_URL", "https://rpc-one.example,https://rpc-two.example")
    settings = Settings.load(config_path=str(config_path), env_path=str(env_path))
    wallet = Wallet.from_env(
        address=settings.config.wallet.address,
        env_path=settings.env_path,
        require_private_key=False,
    )

    assert settings.env_path == env_path.resolve()
    assert settings.config.networks["polygon"].rpc_urls == ["https://rpc-one.example", "https://rpc-two.example"]
    assert wallet.private_key == "0xabc123"
    assert wallet.has_private_key is True


def test_settings_ignores_placeholder_rpc_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app:
  mode: paper_trading
  emergency_stop_file: "./stop.flag"
  dry_run: true
  execute_transactions: false
  sign_transactions: false
  paper_trading: true
  paper:
    enabled: true
    state_file: "./paper_state.json"
    execution_journal_file: "./journal.json"
    initial_holdings:
      USDC: 1000000
    accrue_yield_on_heartbeat: true
networks:
  polygon:
    chain_id: 137
    name: "Polygon"
    rpc_urls:
      - "https://polygon.publicnode.com"
wallet:
  address: "0x000000000000000000000000000000000000dEaD"
assets:
  stablecoins:
    - USDC
protocols:
  enabled:
    - aave
execution:
  gas:
    max_gas_volatility_bps: 300
    max_gwei: 250
    priority_fee_gwei: 1.5
    gas_limit_buffer: 1.15
  slippage:
    bps: 20
  cooldown_seconds: 1800
  min_profit_usd: 3.0
scheduler:
  timezone: "UTC"
  monitor_interval_seconds: 60
  rebalance_interval_seconds: 300
  healthcheck_interval_seconds: 120
  heartbeat_interval_seconds: 30
risk:
  rate_limit_per_minute: 120
  max_consecutive_failures: 10
apy:
  cache_ttl_seconds: 20
  timeout_seconds: 10
""".strip(),
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("RPC_POLYGON_URL", "https://YOUR_RPC_ENDPOINT")
    settings = Settings.load(config_path=str(config_path), env_path=str(env_path))

    assert settings.config.networks["polygon"].rpc_urls == ["https://polygon.publicnode.com"]
