from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen


class BotmasterStatusClient:
    def __init__(self) -> None:
        base_url = os.getenv("BOTMASTER_DASHBOARD_URL", "https://botmaster-l17d.onrender.com")
        self.endpoint = os.getenv("BOTMASTER_STATUS_ENDPOINT", f"{base_url.rstrip('/')}/api/ingest")
        self.token = os.getenv("BOTMASTER_STATUS_TOKEN", "")

    def write(
        self,
        status: str,
        metrics: dict[str, object] | None = None,
        error: str | None = None,
    ) -> bool:
        if not self.token:
            return False
        payload = {
            "bot_id": "shortmaster",
            "bot_name": "ShortMaster",
            "status": status.upper(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics or {},
            "error": error,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-BotMaster-Token": self.token,
            },
        )
        try:
            with urlopen(request, timeout=8):
                return True
        except (OSError, URLError):
            return False
