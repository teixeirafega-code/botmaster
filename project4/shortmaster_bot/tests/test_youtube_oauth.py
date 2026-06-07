from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.publisher.youtube import YouTubePublishError, YouTubePublisher
from app.services.config import load_config
from tests.conftest import make_test_config


def write_installed_client(path: Path, client_id: str = "desktop-client.apps.googleusercontent.com") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": client_id,
                    "project_id": "shortsmaster-test",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_secret": "test-secret",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )


def write_web_client(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "web-client.apps.googleusercontent.com",
                    "project_id": "shortsmaster-test",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_secret": "test-secret",
                    "redirect_uris": ["https://example.com/oauth2callback"],
                }
            }
        ),
        encoding="utf-8",
    )


def test_installed_oauth_client_secret_is_detected_safely(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    secret_path = tmp_path / "secrets" / "client.json"
    write_installed_client(secret_path)

    info = YouTubePublisher(config).inspect_oauth_client_secret()

    assert info["client_type"] == "installed"
    assert info["is_desktop_app"] is True
    assert info["has_client_id"] is True
    assert info["has_client_secret"] is True
    assert info["redirect_uri_count"] == 1
    assert "client_secret" not in info
    assert "_client_id" not in info


def test_web_oauth_client_secret_fails_with_clear_error(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    secret_path = tmp_path / "secrets" / "client.json"
    write_web_client(secret_path)

    with pytest.raises(YouTubePublishError, match="Web Application credentials"):
        YouTubePublisher(config).inspect_oauth_client_secret()


def test_stale_token_is_archived_when_oauth_client_changes(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    secret_path = tmp_path / "secrets" / "client.json"
    token_path = tmp_path / "secrets" / "token.json"
    write_installed_client(secret_path, client_id="new-client.apps.googleusercontent.com")
    token_path.write_text(
        json.dumps(
            {
                "token": "token",
                "refresh_token": "refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "old-client.apps.googleusercontent.com",
                "client_secret": "old-secret",
                "scopes": YouTubePublisher.SCOPES,
            }
        ),
        encoding="utf-8",
    )
    publisher = YouTubePublisher(config)
    info = publisher._load_client_secret_info(secret_path)

    publisher._discard_stale_token_if_client_changed(token_path, info)

    assert not token_path.exists()
    assert list((tmp_path / "secrets").glob("token.json.client-changed.*.bak"))


def test_local_server_is_default_oauth_mode(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("app:\n  paper_mode: true\n", encoding="utf-8")

    config = load_config(tmp_path)

    assert config["publishing"]["oauth_mode"] == "local_server"
