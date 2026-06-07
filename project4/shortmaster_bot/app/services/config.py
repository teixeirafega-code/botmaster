from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when ShortsMaster configuration cannot be loaded."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def load_config(root_dir: Path | None = None) -> dict[str, Any]:
    root = root_dir or ROOT_DIR
    load_dotenv(root / ".env")
    config_path = Path(os.getenv("SHORTSMASTER_CONFIG", root / "config.yaml"))
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        file_config = yaml.safe_load(handle) or {}

    defaults: dict[str, Any] = {
        "root_dir": str(root),
        "app": {
            "name": "ShortsMaster Bot",
            "timezone": "America/Sao_Paulo",
            "paper_mode": True,
            "log_level": "INFO",
            "database_path": "data/shortsmaster.db",
        },
        "scheduler": {
            "trend_interval_minutes": 30,
            "metrics_interval_minutes": 120,
            "heartbeat_interval_minutes": 5,
            "upload_slots_per_day": 5,
            "upload_window_start_hour": 8,
            "upload_window_end_hour": 22,
            "max_consecutive_upload_failures": 3,
            "max_upload_attempts_per_video": 3,
            "validation_failure_window": 10,
            "validation_failure_min_events": 5,
            "max_validation_failure_rate": 0.5,
        },
        "queue": {
            "manual_approval": True,
            "max_pending": 25,
            "auto_process_approved": True,
            "auto_approve_live_upload": False,
        },
        "storage": {
            "base_dir": "",
            "videos_dir": "videos",
            "reports_dir": "reports",
            "logs_dir": "logs",
        },
        "language": {
            "default": "pt-BR",
            "required": "pt-BR",
            "block_english_output": True,
        },
        "trends": {
            "tiktok": {
                "enabled": False,
                "provider": "disabled",
            },
        },
        "story_mode": {
            "enabled": False,
            "user_agent": "ShortsMasterBot/1.0 reddit story shorts mode",
            "subreddits": [
                "AskReddit",
                "TodayILearned",
                "LetsNotMeet",
                "NoStupidQuestions",
                "LifeProTips",
                "TIFU",
                "InterestingAsFuck",
            ],
            "sort": "top",
            "time_filter": "week",
            "limit_per_subreddit": 25,
            "min_upvotes": 2500,
            "min_comments": 120,
            "min_comment_score": 250,
            "min_story_chars": 450,
            "min_story_words": 90,
            "use_top_comments": True,
            "requires_fresh_source": True,
            "freshness_window_hours": 168,
            "freshness_min_score": 70,
            "trust_min_score": 70,
            "background_video": {
                "mode": "custom_background_library",
                "royalty_free_only": True,
                "loop": True,
                "subtitles": "always",
                "library_dir": "background_library",
                "manifest_path": "background_library_manifest.json",
                "loop_seconds": 8,
                "subtitle_words_per_chunk": 7,
                "approved_categories": {
                    "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
                    "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
                },
            },
        },
        "generation": {
            "script": {
                "provider_order": ["ollama", "template"],
                "target_seconds": 60,
                "retention_scene_count": 10,
                "max_tokens": 1200,
                "temperature": 0.7,
                "ollama": {
                    "enabled": True,
                    "base_url": "http://localhost:11434",
                    "model": "llama3.1:8b",
                    "timeout_seconds": 6,
                },
            },
            "voice": {
                "provider": "edge_tts",
                "default_profile": "neutral_storyteller",
                "allow_gtts_fallback": True,
                "lang": "pt",
                "tld": "com.br",
                "slow": False,
            },
        },
        "research": {
            "wikipedia_enabled": True,
            "timeout_seconds": 12,
            "current_topic_freshness_enabled": True,
            "freshness_window_hours": 72,
            "freshness_min_score": 70,
            "trust_min_score": 70,
        },
        "publishing": {
            "live_upload_enabled": False,
            "enable_real_upload": False,
            "daily_upload_limit": 5,
            "max_daily_upload_limit": 5,
            "min_upload_quality_score": 75,
            "min_upload_safety_score": 90,
            "youtube_daily_quota_units": 10000,
            "youtube_upload_quota_units": 1600,
            "channel_id": "",
            "channel_name": "",
            "youtube_client_secrets_file": "secrets/youtube_client_secret.json",
            "youtube_token_file": "secrets/youtube_token.json",
            "youtube_client_secret_json_env": "YOUTUBE_CLIENT_SECRET_JSON",
            "youtube_token_json_env": "YOUTUBE_TOKEN_JSON",
            "oauth_mode": "local_server",
            "privacy_status": "private",
        },
        "safety": {
            "min_quality_score": 70,
            "min_script_words": 105,
            "max_script_words": 190,
            "min_scene_count": 8,
            "min_hook_score": 70,
            "min_retention_score": 72,
            "min_visual_interest_score": 70,
            "min_narrative_naturalness_score": 75,
            "source_copy_ngram_words": 8,
        },
        "monitoring": {
            "telegram": {
                "enabled": False,
                "bot_token": "",
                "chat_id": "",
                "timeout_seconds": 10,
            },
        },
    }

    config = _deep_merge(defaults, file_config)
    config["app"]["paper_mode"] = _env_bool("PAPER_MODE", config["app"]["paper_mode"])
    config["app"]["log_level"] = os.getenv("LOG_LEVEL", config["app"]["log_level"])
    config["app"]["database_path"] = os.getenv(
        "DATABASE_PATH",
        config["app"]["database_path"],
    )
    manual_default = config["queue"]["manual_approval"]
    manual_default = _env_bool("MANUAL_APPROVAL", manual_default)
    config["queue"]["manual_approval"] = _env_bool("MANUAL_APPROVAL_REQUIRED", manual_default)
    config["queue"]["auto_approve_live_upload"] = _env_bool(
        "AUTO_APPROVE_LIVE_UPLOAD",
        bool(config["queue"].get("auto_approve_live_upload", False)),
    )
    config["scheduler"]["trend_interval_minutes"] = _env_int(
        "TREND_INTERVAL_MINUTES",
        int(config["scheduler"]["trend_interval_minutes"]),
    )
    config["scheduler"]["metrics_interval_minutes"] = _env_int(
        "METRICS_INTERVAL_MINUTES",
        int(config["scheduler"].get("metrics_interval_minutes", 120)),
    )
    config["scheduler"]["heartbeat_interval_minutes"] = _env_int(
        "HEARTBEAT_INTERVAL_MINUTES",
        int(config["scheduler"].get("heartbeat_interval_minutes", 5)),
    )
    config["scheduler"]["upload_slots_per_day"] = _env_int(
        "UPLOAD_SLOTS_PER_DAY",
        int(config["scheduler"].get("upload_slots_per_day", 5)),
    )

    config.setdefault("storage", {})
    config["storage"]["base_dir"] = os.getenv(
        "SHORTSMASTER_STORAGE_DIR",
        config["storage"].get("base_dir", ""),
    )
    config["storage"]["videos_dir"] = os.getenv(
        "VIDEOS_DIR",
        config["storage"].get("videos_dir", "videos"),
    )
    config["storage"]["reports_dir"] = os.getenv(
        "REPORTS_DIR",
        config["storage"].get("reports_dir", "reports"),
    )
    config["storage"]["logs_dir"] = os.getenv(
        "LOGS_DIR",
        config["storage"].get("logs_dir", "logs"),
    )

    config.setdefault("generation", {})
    config["generation"].setdefault("script", {})
    config["generation"].setdefault("voice", {})
    config["generation"]["script"].setdefault("ollama", {})
    config["generation"]["script"]["retention_scene_count"] = _env_int(
        "RETENTION_SCENE_COUNT",
        int(config["generation"]["script"].get("retention_scene_count", 10)),
    )
    config["generation"]["script"]["ollama"]["enabled"] = _env_bool(
        "OLLAMA_SCRIPT_ENABLED",
        bool(config["generation"]["script"]["ollama"].get("enabled", True)),
    )
    config["generation"]["script"]["ollama"]["base_url"] = os.getenv(
        "OLLAMA_BASE_URL",
        config["generation"]["script"]["ollama"].get("base_url", "http://localhost:11434"),
    )
    config["generation"]["script"]["ollama"]["model"] = os.getenv(
        "OLLAMA_MODEL",
        config["generation"]["script"]["ollama"].get("model", "llama3.1:8b"),
    )
    config.setdefault("generation", {}).setdefault("video", {})
    if os.getenv("MAX_VISUAL_BEAT_SECONDS"):
        config["generation"]["video"]["max_visual_beat_seconds"] = float(os.getenv("MAX_VISUAL_BEAT_SECONDS", "2.0"))
    config.setdefault("language", {})
    config["language"]["default"] = os.getenv("SHORTSMASTER_DEFAULT_LANGUAGE", config["language"].get("default", "pt-BR"))
    config["language"]["required"] = os.getenv("SHORTSMASTER_REQUIRED_LANGUAGE", config["language"].get("required", "pt-BR"))
    config["language"]["block_english_output"] = _env_bool(
        "SHORTSMASTER_BLOCK_ENGLISH_OUTPUT",
        bool(config["language"].get("block_english_output", True)),
    )
    config["generation"]["voice"]["lang"] = os.getenv(
        "VOICE_LANGUAGE",
        config["generation"]["voice"].get("lang", "pt"),
    )
    config["generation"]["voice"]["tld"] = os.getenv(
        "VOICE_TLD",
        config["generation"]["voice"].get("tld", "com.br"),
    )
    config["generation"]["voice"]["provider"] = os.getenv(
        "VOICE_PROVIDER",
        config["generation"]["voice"].get("provider", "edge_tts"),
    )
    config["generation"]["voice"]["default_profile"] = os.getenv(
        "VOICE_PROFILE",
        config["generation"]["voice"].get("default_profile", "neutral_storyteller"),
    )
    config["generation"]["voice"]["allow_gtts_fallback"] = _env_bool(
        "VOICE_ALLOW_GTTS_FALLBACK",
        bool(config["generation"]["voice"].get("allow_gtts_fallback", True)),
    )

    config.setdefault("trends", {})
    config["trends"].setdefault("tiktok", {})
    config["trends"]["tiktok"]["enabled"] = _env_bool(
        "TIKTOK_MONITOR_ENABLED",
        bool(config["trends"]["tiktok"].get("enabled", False)),
    )
    config["trends"]["tiktok"]["provider"] = os.getenv(
        "TIKTOK_PROVIDER",
        config["trends"]["tiktok"].get("provider", "disabled"),
    )

    config.setdefault("story_mode", {})
    config["story_mode"]["enabled"] = _env_bool(
        "REDDIT_STORY_MODE_ENABLED",
        bool(config["story_mode"].get("enabled", False)),
    )
    config["story_mode"]["min_upvotes"] = _env_int(
        "REDDIT_STORY_MIN_UPVOTES",
        int(config["story_mode"].get("min_upvotes", 2500)),
    )
    config["story_mode"]["min_comments"] = _env_int(
        "REDDIT_STORY_MIN_COMMENTS",
        int(config["story_mode"].get("min_comments", 120)),
    )
    config["story_mode"]["limit_per_subreddit"] = _env_int(
        "REDDIT_STORY_LIMIT_PER_SUBREDDIT",
        int(config["story_mode"].get("limit_per_subreddit", 25)),
    )
    config["story_mode"]["freshness_window_hours"] = _env_int(
        "REDDIT_STORY_FRESHNESS_WINDOW_HOURS",
        int(config["story_mode"].get("freshness_window_hours", 168)),
    )
    config["story_mode"].setdefault("background_video", {})
    config["story_mode"]["background_video"]["mode"] = os.getenv(
        "REDDIT_STORY_BACKGROUND_MODE",
        config["story_mode"]["background_video"].get("mode", "custom_background_library"),
    )
    config["story_mode"]["background_video"]["library_dir"] = os.getenv(
        "BACKGROUND_LIBRARY_DIR",
        config["story_mode"]["background_video"].get("library_dir", "background_library"),
    )
    config["story_mode"]["background_video"]["manifest_path"] = os.getenv(
        "BACKGROUND_LIBRARY_MANIFEST",
        config["story_mode"]["background_video"].get("manifest_path", "background_library_manifest.json"),
    )

    config.setdefault("publishing", {})
    config["publishing"]["live_upload_enabled"] = _env_bool(
        "LIVE_UPLOAD_ENABLED",
        bool(config["publishing"].get("live_upload_enabled", False)),
    )
    config["publishing"]["enable_real_upload"] = _env_bool(
        "ENABLE_REAL_UPLOAD",
        bool(config["publishing"].get("enable_real_upload", False)),
    )
    config["publishing"]["daily_upload_limit"] = _env_int(
        "DAILY_UPLOAD_LIMIT",
        int(config["publishing"].get("daily_upload_limit", 1)),
    )
    config["publishing"]["max_daily_upload_limit"] = _env_int(
        "MAX_DAILY_UPLOAD_LIMIT",
        int(config["publishing"].get("max_daily_upload_limit", 5)),
    )
    config["publishing"]["daily_upload_limit"] = min(
        int(config["publishing"]["daily_upload_limit"]),
        int(config["publishing"]["max_daily_upload_limit"]),
    )
    config["publishing"]["min_upload_quality_score"] = _env_int(
        "MIN_UPLOAD_QUALITY_SCORE",
        int(config["publishing"].get("min_upload_quality_score", 75)),
    )
    config["publishing"]["min_upload_safety_score"] = _env_int(
        "MIN_UPLOAD_SAFETY_SCORE",
        int(config["publishing"].get("min_upload_safety_score", 90)),
    )
    config["publishing"]["youtube_daily_quota_units"] = _env_int(
        "YOUTUBE_DAILY_QUOTA_UNITS",
        int(config["publishing"].get("youtube_daily_quota_units", 10000)),
    )
    config["publishing"]["youtube_upload_quota_units"] = _env_int(
        "YOUTUBE_UPLOAD_QUOTA_UNITS",
        int(config["publishing"].get("youtube_upload_quota_units", 1600)),
    )
    config["publishing"]["channel_id"] = os.getenv(
        "YOUTUBE_CHANNEL_ID",
        config["publishing"].get("channel_id", ""),
    )
    config["publishing"]["channel_name"] = os.getenv(
        "YOUTUBE_CHANNEL_NAME",
        config["publishing"].get("channel_name", ""),
    )
    config["publishing"]["youtube_client_secrets_file"] = os.getenv(
        "YOUTUBE_CLIENT_SECRETS_FILE",
        config["publishing"].get("youtube_client_secrets_file", "secrets/youtube_client_secret.json"),
    )
    config["publishing"]["youtube_token_file"] = os.getenv(
        "YOUTUBE_TOKEN_FILE",
        config["publishing"].get("youtube_token_file", "secrets/youtube_token.json"),
    )
    config["publishing"]["youtube_client_secret_json_env"] = os.getenv(
        "YOUTUBE_CLIENT_SECRET_JSON_ENV",
        config["publishing"].get("youtube_client_secret_json_env", "YOUTUBE_CLIENT_SECRET_JSON"),
    )
    config["publishing"]["youtube_token_json_env"] = os.getenv(
        "YOUTUBE_TOKEN_JSON_ENV",
        config["publishing"].get("youtube_token_json_env", "YOUTUBE_TOKEN_JSON"),
    )
    config["publishing"]["oauth_mode"] = os.getenv(
        "YOUTUBE_OAUTH_MODE",
        config["publishing"].get("oauth_mode", "local_server"),
    )
    config["publishing"]["privacy_status"] = os.getenv(
        "YOUTUBE_PRIVACY_STATUS",
        config["publishing"].get("privacy_status", "private"),
    )

    deployment_environment = os.getenv("SHORTSMASTER_ENV", "development").strip().lower()
    config["app"]["environment"] = deployment_environment
    if not config["app"]["paper_mode"] and deployment_environment != "production":
        raise ConfigError("PAPER_MODE=false is allowed only when SHORTSMASTER_ENV=production")
    if config["publishing"]["enable_real_upload"] or config["publishing"]["live_upload_enabled"]:
        if deployment_environment != "production":
            raise ConfigError("Real upload flags are allowed only when SHORTSMASTER_ENV=production")
        if int(config["publishing"]["min_upload_quality_score"]) < 75:
            raise ConfigError("Production min_upload_quality_score cannot be lower than 75")
        if int(config["publishing"]["min_upload_safety_score"]) < 90:
            raise ConfigError("Production min_upload_safety_score cannot be lower than 90")

    config.setdefault("safety", {})
    config["safety"]["min_scene_count"] = _env_int(
        "MIN_SCENE_COUNT",
        int(config["safety"].get("min_scene_count", 8)),
    )
    config["safety"]["min_hook_score"] = _env_int(
        "MIN_HOOK_SCORE",
        int(config["safety"].get("min_hook_score", 70)),
    )
    config["safety"]["min_retention_score"] = _env_int(
        "MIN_RETENTION_SCORE",
        int(config["safety"].get("min_retention_score", 72)),
    )
    config["safety"]["min_visual_interest_score"] = _env_int(
        "MIN_VISUAL_INTEREST_SCORE",
        int(config["safety"].get("min_visual_interest_score", 70)),
    )
    config.setdefault("monitoring", {})
    config["monitoring"].setdefault("telegram", {})
    config["monitoring"]["telegram"]["enabled"] = _env_bool(
        "TELEGRAM_ALERTS_ENABLED",
        bool(config["monitoring"]["telegram"].get("enabled", False)),
    )
    config["monitoring"]["telegram"]["bot_token"] = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        config["monitoring"]["telegram"].get("bot_token", ""),
    )
    config["monitoring"]["telegram"]["chat_id"] = os.getenv(
        "TELEGRAM_CHAT_ID",
        config["monitoring"]["telegram"].get("chat_id", ""),
    )
    return config


def resolve_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(config["root_dir"]) / path


def resolve_storage_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    base_dir = str(config.get("storage", {}).get("base_dir", "") or "")
    if base_dir:
        return Path(base_dir) / path
    return resolve_path(config, path)
