from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import ContentScript, utc_now_iso
from app.services.config import resolve_path


LOGGER = logging.getLogger(__name__)


class YouTubePublishError(RuntimeError):
    """Raised when YouTube upload or API access fails."""


class YouTubePublisher:
    SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]

    def __init__(self, config: dict):
        self.config = config
        self.publish_config = config.get("publishing", {})
        self.paper_mode = bool(config.get("app", {}).get("paper_mode", True))
        self.live_upload_enabled = bool(self.publish_config.get("live_upload_enabled", False))
        self.real_upload_enabled = (
            bool(self.publish_config.get("enable_real_upload", False))
            and self.live_upload_enabled
            and not self.paper_mode
        )
        self._service: Any | None = None

    def publish(self, video_path: Path, script: ContentScript, queue_item: dict[str, Any]) -> str:
        if not self.real_upload_enabled:
            paper_id = f"paper-{queue_item['id']}-{utc_now_iso().replace(':', '').replace('+', 'Z')}"
            LOGGER.info("Real upload disabled; simulating YouTube publish as %s", paper_id)
            return paper_id

        if not video_path.exists():
            raise YouTubePublishError(f"Video file not found: {video_path}")
        if not self.publish_config.get("channel_id"):
            raise YouTubePublishError("YouTube channel_id must be configured before live upload")

        service = self._get_service()
        body = {
            "snippet": {
                "title": script.title[:100],
                "description": self._description(script),
                "tags": script.tags[:15],
                "categoryId": str(self.publish_config.get("category_id", "22")),
            },
            "status": {
                "privacyStatus": self.publish_config.get("privacy_status", "private"),
                "selfDeclaredMadeForKids": bool(self.publish_config.get("made_for_kids", False)),
            },
        }

        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise YouTubePublishError("google-api-python-client is not installed") from exc

        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                LOGGER.info("YouTube upload progress: %.1f%%", status.progress() * 100)
        video_id = response.get("id")
        if not video_id:
            raise YouTubePublishError(f"YouTube upload response did not include id: {response}")
        LOGGER.info("Uploaded YouTube video id %s", video_id)
        return str(video_id)

    def fetch_stats(self, video_ids: list[str]) -> dict[str, dict[str, int]]:
        if not video_ids:
            return {}
        if not self.real_upload_enabled:
            return {}

        service = self._get_service()
        response = (
            service.videos()
            .list(part="statistics", id=",".join(video_ids[:50]))
            .execute()
        )
        stats: dict[str, dict[str, int]] = {}
        for item in response.get("items", []):
            values = item.get("statistics", {})
            stats[item["id"]] = {
                "views": int(values.get("viewCount", 0)),
                "likes": int(values.get("likeCount", 0)),
                "comments": int(values.get("commentCount", 0)),
            }
        return stats

    def detect_authenticated_channel(self) -> dict[str, Any]:
        service = self._get_service()
        response = (
            service.channels()
            .list(part="id,snippet", mine=True, maxResults=1)
            .execute()
        )
        items = response.get("items", [])
        if not items:
            raise YouTubePublishError("No YouTube channel was returned for the authenticated account")
        channel = items[0]
        snippet = channel.get("snippet", {})
        configured_channel_id = str(self.publish_config.get("channel_id", "") or "")
        return {
            "authenticated_channel_id": str(channel.get("id", "")),
            "authenticated_channel_title": str(snippet.get("title", "")),
            "authenticated_channel_handle": str(snippet.get("customUrl", "")),
            "configured_channel_id": configured_channel_id,
            "configured_channel_name": str(self.publish_config.get("channel_name", "") or ""),
            "matches_configured_channel": bool(configured_channel_id)
            and configured_channel_id == str(channel.get("id", "")),
            "upload_attempted": False,
        }

    def _description(self, script: ContentScript) -> str:
        hashtags = " ".join(f"#{tag.lstrip('#')}" for tag in script.tags[:4])
        body = script.description.strip()
        if hashtags and hashtags not in body:
            body = f"{body}\n\n{hashtags}"
        return body[:5000]

    def _get_service(self):
        if self._service is not None:
            return self._service

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise YouTubePublishError("Google YouTube API packages are not installed") from exc

        token_path = resolve_path(self.config, self.publish_config["youtube_token_file"])
        secrets_path = resolve_path(self.config, self.publish_config["youtube_client_secrets_file"])
        token_path.parent.mkdir(parents=True, exist_ok=True)
        client_secret_info = self._load_client_secret_info(secrets_path)
        self._log_client_secret_info(client_secret_info)
        token_payload = self._load_token_payload_from_env(client_secret_info)
        if token_payload is None:
            self._discard_stale_token_if_client_changed(token_path, client_secret_info)

        credentials = None
        if token_payload is not None:
            try:
                credentials = Credentials.from_authorized_user_info(token_payload, self.SCOPES)
                LOGGER.info(
                    "Loaded YouTube OAuth token from environment variable %s",
                    self.publish_config.get("youtube_token_json_env", "YOUTUBE_TOKEN_JSON"),
                )
            except Exception as exc:  # pragma: no cover - google package exception types vary
                raise YouTubePublishError("YouTube OAuth token JSON from environment is invalid") from exc
        elif token_path.exists():
            try:
                credentials = Credentials.from_authorized_user_file(str(token_path), self.SCOPES)
            except Exception as exc:  # pragma: no cover - google package exception types vary
                LOGGER.warning("Ignoring invalid YouTube OAuth token at %s: %s", token_path, exc)
                self._archive_token(token_path, "invalid")
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:  # pragma: no cover - depends on live Google response
                LOGGER.warning("Ignoring unrefreshable YouTube OAuth token at %s: %s", token_path, exc)
                self._archive_token(token_path, "refresh-failed")
                credentials = None
        if credentials is None or not credentials.valid:
            flow = InstalledAppFlow.from_client_config(client_secret_info["_payload"], self.SCOPES)
            oauth_mode = str(self.publish_config.get("oauth_mode", "local_server") or "local_server")
            if oauth_mode == "disabled":
                raise YouTubePublishError(
                    "YouTube OAuth bootstrap is disabled. On Render, set YOUTUBE_TOKEN_JSON to a valid "
                    "authorized-user token generated locally; the worker cannot open an interactive OAuth flow."
                )
            if os.getenv("RENDER") and oauth_mode in {"local_server", "console"}:
                raise YouTubePublishError(
                    "Interactive YouTube OAuth is not allowed on Render workers. Set YOUTUBE_OAUTH_MODE=disabled "
                    "and provide YOUTUBE_TOKEN_JSON."
                )
            if oauth_mode == "local_server":
                LOGGER.info("Starting YouTube Desktop OAuth flow with local server redirect on port 0")
                credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
            elif oauth_mode == "console":
                LOGGER.info("Starting YouTube Desktop OAuth console flow")
                credentials = self._run_console_oauth(flow)
            else:
                raise YouTubePublishError(
                    f"Unsupported YouTube OAuth mode '{oauth_mode}'. Use 'local_server', 'console', or 'disabled'."
                )
            token_path.write_text(credentials.to_json(), encoding="utf-8")
            LOGGER.info("Saved YouTube OAuth token to %s", token_path)

        self._service = build(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
        return self._service

    def inspect_oauth_client_secret(self) -> dict[str, Any]:
        """Return safe metadata about the configured OAuth client secret."""
        secrets_path = resolve_path(self.config, self.publish_config["youtube_client_secrets_file"])
        return self._safe_client_secret_info(self._load_client_secret_info(secrets_path))

    def _load_client_secret_info(self, secrets_path: Path) -> dict[str, Any]:
        env_name = str(self.publish_config.get("youtube_client_secret_json_env", "YOUTUBE_CLIENT_SECRET_JSON"))
        payload = self._json_from_env(env_name)
        source = f"env:{env_name}" if payload is not None else str(secrets_path)
        if payload is None:
            if not secrets_path.exists():
                raise YouTubePublishError(
                    f"YouTube OAuth client secret not found. Set {env_name} in the environment "
                    f"or provide an uncommitted local file at {secrets_path}."
                )
            try:
                payload = json.loads(secrets_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise YouTubePublishError(f"YouTube OAuth client secret file is not valid JSON: {secrets_path}") from exc

        client_type = "unknown"
        client_config: dict[str, Any] = {}
        if isinstance(payload, dict) and isinstance(payload.get("installed"), dict):
            client_type = "installed"
            client_config = payload["installed"]
        elif isinstance(payload, dict) and isinstance(payload.get("web"), dict):
            client_type = "web"
            client_config = payload["web"]

        info = {
            "path": source,
            "client_type": client_type,
            "is_desktop_app": client_type == "installed",
            "has_client_id": bool(client_config.get("client_id")),
            "has_client_secret": bool(client_config.get("client_secret")),
            "redirect_uri_count": len(client_config.get("redirect_uris") or []),
            "_client_id": str(client_config.get("client_id", "") or ""),
            "_payload": payload,
        }

        if client_type == "web":
            raise YouTubePublishError(
                "YouTube OAuth client secret is Web Application credentials. "
                "Create a Google Cloud OAuth 'Desktop app' client, download its JSON, "
                f"and provide it through {env_name} or {secrets_path}. "
                "Web clients are not supported by the installed-app OAuth flow."
            )
        if client_type != "installed":
            raise YouTubePublishError(
                "YouTube OAuth client secret must contain an 'installed' Desktop App client. "
                f"Loaded client_type='{client_type}' from {source}."
            )
        if not info["has_client_id"] or not info["has_client_secret"]:
            raise YouTubePublishError(
                f"YouTube OAuth Desktop App client secret is missing client_id or client_secret: {source}"
            )
        return info

    def _load_token_payload_from_env(self, client_secret_info: dict[str, Any]) -> dict[str, Any] | None:
        env_name = str(self.publish_config.get("youtube_token_json_env", "YOUTUBE_TOKEN_JSON"))
        payload = self._json_from_env(env_name)
        if payload is None:
            return None
        token_client_id = str(payload.get("client_id", "") or "")
        secret_client_id = str(client_secret_info.get("_client_id", "") or "")
        if not token_client_id:
            raise YouTubePublishError(f"YouTube OAuth token in {env_name} is missing client_id")
        if secret_client_id and token_client_id != secret_client_id:
            raise YouTubePublishError(
                f"YouTube OAuth token in {env_name} was generated for a different OAuth client"
            )
        return payload

    def _json_from_env(self, env_name: str) -> dict[str, Any] | None:
        raw = os.getenv(env_name)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise YouTubePublishError(f"Environment variable {env_name} must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise YouTubePublishError(f"Environment variable {env_name} must contain a JSON object")
        return payload

    def _log_client_secret_info(self, info: dict[str, Any]) -> None:
        safe_info = self._safe_client_secret_info(info)
        LOGGER.info(
            "Loaded YouTube OAuth client secret: client_type=%s desktop_app=%s redirect_uri_count=%s "
            "has_client_id=%s has_client_secret=%s path=%s",
            safe_info["client_type"],
            safe_info["is_desktop_app"],
            safe_info["redirect_uri_count"],
            safe_info["has_client_id"],
            safe_info["has_client_secret"],
            safe_info["path"],
        )

    def _safe_client_secret_info(self, info: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in info.items() if not key.startswith("_")}

    def _discard_stale_token_if_client_changed(self, token_path: Path, client_secret_info: dict[str, Any]) -> None:
        if not token_path.exists():
            return
        try:
            token_payload = json.loads(token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring unreadable YouTube OAuth token at %s", token_path)
            self._archive_token(token_path, "unreadable")
            return

        token_client_id = str(token_payload.get("client_id", "") or "")
        secret_client_id = str(client_secret_info.get("_client_id", "") or "")
        if not token_client_id:
            LOGGER.warning("Ignoring YouTube OAuth token without client_id at %s", token_path)
            self._archive_token(token_path, "missing-client-id")
            return
        if secret_client_id and token_client_id != secret_client_id:
            LOGGER.warning("Ignoring stale YouTube OAuth token because OAuth client changed: %s", token_path)
            self._archive_token(token_path, "client-changed")

    def _archive_token(self, token_path: Path, reason: str) -> Path | None:
        if not token_path.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = token_path.with_name(f"{token_path.name}.{reason}.{timestamp}.bak")
        token_path.replace(archive_path)
        LOGGER.info("Archived ignored YouTube OAuth token at %s", archive_path)
        return archive_path

    def _run_console_oauth(self, flow):
        helper = getattr(flow, "run_console", None)
        if callable(helper):
            return helper()
        raise YouTubePublishError(
            "Console OAuth flow is unavailable in the installed google-auth-oauthlib version. "
            "Set publishing.oauth_mode=local_server so InstalledAppFlow can provide a localhost redirect_uri."
        )
