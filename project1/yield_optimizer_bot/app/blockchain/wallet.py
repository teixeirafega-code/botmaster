from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from eth_account import Account


@dataclass(frozen=True)
class WatchOnlyAccount:
    address: str

    def sign_transaction(self, _tx):
        raise RuntimeError("Signing is disabled for watch-only / dry-run wallet")


@dataclass(frozen=True)
class Wallet:
    address: str
    private_key: str | None

    @staticmethod
    def _normalize_secret(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().strip("\"'")
        return normalized or None

    @classmethod
    def _read_private_key_from_env_file(cls, env_path: str | Path | None, private_key_env: str) -> str | None:
        if env_path is None:
            return None
        path = Path(env_path)
        if not path.exists():
            return None
        return cls._normalize_secret(dotenv_values(path).get(private_key_env))

    @classmethod
    def from_env(
        cls,
        address: str,
        private_key_env: str = "PRIVATE_KEY",
        env_path: str | Path | None = None,
        *,
        require_private_key: bool = True,
    ) -> "Wallet":
        pk = cls._normalize_secret(os.getenv(private_key_env))
        if not pk:
            pk = cls._read_private_key_from_env_file(env_path, private_key_env)
        if require_private_key and not pk:
            raise RuntimeError(f"Missing environment variable: {private_key_env}")
        return cls(address=address, private_key=pk)

    @property
    def has_private_key(self) -> bool:
        return bool(self.private_key)

    def account(self):
        if not self.private_key:
            return WatchOnlyAccount(address=self.address)
        return Account.from_key(self.private_key)
