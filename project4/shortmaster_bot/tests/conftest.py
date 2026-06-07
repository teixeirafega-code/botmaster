from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_test_config(tmp_path: Path) -> dict:
    return {
        "root_dir": str(tmp_path),
        "app": {
            "paper_mode": True,
            "database_path": "shortsmaster-test.db",
            "timezone": "UTC",
            "log_level": "INFO",
        },
        "queue": {
            "manual_approval": True,
            "max_pending": 25,
            "auto_process_approved": True,
        },
        "language": {
            "default": "pt-BR",
            "required": "pt-BR",
            "block_english_output": True,
        },
        "selection": {
            "default_niche": "general",
            "source_weights": {"reddit": 1.0, "google_trends": 1.2, "tiktok": 1.1},
            "niches": {
                "technology": ["ai", "software", "robot"],
                "finance": ["stock", "money", "bitcoin"],
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
                    "enabled": False,
                    "base_url": "http://localhost:11434",
                    "model": "llama3.1:8b",
                    "timeout_seconds": 0.1,
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
            "images": {"width": 1080, "height": 1920, "offline_fallback": True},
            "video": {"width": 1080, "height": 1920, "fps": 30, "max_visual_beat_seconds": 2.0},
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
            "youtube_client_secrets_file": "secrets/client.json",
            "youtube_token_file": "secrets/token.json",
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
            "script_similarity_block_threshold": 0.92,
            "performance_guard": {
                "enabled": True,
                "min_recent_videos": 3,
                "recent_video_window": 5,
                "min_avg_views": 50,
                "min_avg_like_rate": 0.01,
            },
        },
    }
