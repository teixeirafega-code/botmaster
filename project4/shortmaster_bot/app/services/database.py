from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import QueueStatus, TrendTopic, utc_now_iso


class ShortsMasterDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_topics (
                    topic_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    first_source TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    selected_at TEXT,
                    niche TEXT NOT NULL DEFAULT 'general'
                );

                CREATE TABLE IF NOT EXISTS trend_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    score REAL NOT NULL,
                    niche TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS content_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    niche TEXT NOT NULL,
                    trend_score REAL NOT NULL,
                    status TEXT NOT NULL,
                    research_json TEXT,
                    script_json TEXT,
                    video_path TEXT,
                    background_category TEXT,
                    background_filename TEXT,
                    background_source_url TEXT,
                    background_license_type TEXT,
                    background_commercial_rights_verified INTEGER NOT NULL DEFAULT 0,
                    quality_score REAL,
                    safety_json TEXT,
                    upload_blocked_reason TEXT,
                    approved_for_live_upload INTEGER NOT NULL DEFAULT 0,
                    live_approved_at TEXT,
                    upload_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_upload_attempt_at TEXT,
                    last_upload_error TEXT,
                    youtube_video_id TEXT,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT,
                    published_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_queue_status
                    ON content_queue(status, created_at);

                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id INTEGER NOT NULL,
                    youtube_video_id TEXT NOT NULL,
                    views INTEGER NOT NULL,
                    likes INTEGER NOT NULL,
                    comments INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    FOREIGN KEY(queue_id) REFERENCES content_queue(id)
                );

                CREATE TABLE IF NOT EXISTS niche_performance (
                    niche TEXT PRIMARY KEY,
                    published_count INTEGER NOT NULL DEFAULT 0,
                    avg_views REAL NOT NULL DEFAULT 0,
                    avg_likes REAL NOT NULL DEFAULT 0,
                    performance_score REAL NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS youtube_quota_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usage_date TEXT NOT NULL,
                    queue_id INTEGER,
                    operation TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(queue_id) REFERENCES content_queue(id)
                );

                CREATE TABLE IF NOT EXISTS youtube_scheduler_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queue_id INTEGER,
                    reason TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(queue_id) REFERENCES content_queue(id)
                );

                CREATE INDEX IF NOT EXISTS idx_scheduler_events_created
                    ON youtube_scheduler_events(created_at);

                CREATE TABLE IF NOT EXISTS scheduler_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS background_library_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_background_library_events_filename
                    ON background_library_events(filename, created_at);
                """
            )
            self._ensure_content_queue_columns()
            self._conn.commit()

    def _ensure_content_queue_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(content_queue)").fetchall()
        }
        additions = {
            "quality_score": "REAL",
            "safety_json": "TEXT",
            "upload_blocked_reason": "TEXT",
            "research_json": "TEXT",
            "background_category": "TEXT",
            "background_filename": "TEXT",
            "background_source_url": "TEXT",
            "background_license_type": "TEXT",
            "background_commercial_rights_verified": "INTEGER NOT NULL DEFAULT 0",
            "approved_for_live_upload": "INTEGER NOT NULL DEFAULT 0",
            "live_approved_at": "TEXT",
            "upload_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_upload_attempt_at": "TEXT",
            "last_upload_error": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE content_queue ADD COLUMN {name} {definition}")

    def row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def is_seen(self, topic_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_topics WHERE topic_key = ?",
                (topic_key,),
            ).fetchone()
            return row is not None

    def mark_seen(self, topic: TrendTopic, selected: bool = False) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO seen_topics(topic_key, title, first_source, first_seen_at, selected_at, niche)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    selected_at = COALESCE(excluded.selected_at, seen_topics.selected_at),
                    niche = excluded.niche
                """,
                (
                    topic.key,
                    topic.title,
                    topic.source,
                    now,
                    now if selected else None,
                    topic.niche,
                ),
            )
            self._conn.commit()

    def record_observations(self, topics: list[TrendTopic]) -> None:
        if not topics:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO trend_observations(
                    topic_key, title, source, score, niche, observed_at, raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        topic.key,
                        topic.title,
                        topic.source,
                        topic.score,
                        topic.niche,
                        topic.observed_at,
                        json.dumps(topic.raw, ensure_ascii=True, sort_keys=True),
                    )
                    for topic in topics
                ],
            )
            self._conn.commit()

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM content_queue WHERE status IN (?, ?, ?, ?)",
                (
                    QueueStatus.PENDING_APPROVAL,
                    QueueStatus.APPROVED,
                    QueueStatus.GENERATING,
                    QueueStatus.READY,
                ),
            ).fetchone()
            return int(row["count"])

    def enqueue_topic(self, topic: TrendTopic, status: str) -> dict[str, Any]:
        now = utc_now_iso()
        payload = topic.to_dict()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO content_queue(
                    topic_key, title, source, niche, trend_score, status,
                    payload_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    topic.key,
                    topic.title,
                    topic.source,
                    topic.niche,
                    topic.score,
                    status,
                    json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return self.get_queue_item_by_topic(topic.key) or {}

    def get_queue_item(self, queue_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM content_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
            return self.row_to_dict(row)

    def get_queue_item_by_topic(self, topic_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM content_queue WHERE topic_key = ?",
                (topic_key,),
            ).fetchone()
            return self.row_to_dict(row)

    def list_queue(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM content_queue"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def list_for_processing(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM content_queue
                WHERE status IN (?, ?)
                ORDER BY approved_at ASC, created_at ASC
                LIMIT ?
                """,
                (QueueStatus.APPROVED, QueueStatus.READY, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def next_upload_candidate(self, max_attempts: int = 3) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM content_queue
                WHERE status IN (?, ?)
                  AND youtube_video_id IS NULL
                  AND approved_for_live_upload = 1
                  AND COALESCE(upload_attempt_count, 0) < ?
                ORDER BY
                  CASE WHEN approved_at IS NULL THEN created_at ELSE approved_at END ASC,
                  created_at ASC,
                  id ASC
                LIMIT 1
                """,
                (QueueStatus.READY, QueueStatus.APPROVED, int(max_attempts)),
            ).fetchone()
            return self.row_to_dict(row)

    def list_published(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM content_queue
                WHERE status = ? AND youtube_video_id IS NOT NULL
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (QueueStatus.PUBLISHED, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def count_real_uploads_today(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM content_queue
                WHERE status = ? AND published_at >= ?
                """,
                (QueueStatus.PUBLISHED, start.isoformat()),
            ).fetchone()
            return int(row["count"])

    def list_recent_script_payloads(
        self,
        exclude_queue_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, title, script_json
            FROM content_queue
            WHERE script_json IS NOT NULL
        """
        params: list[Any] = []
        if exclude_queue_id is not None:
            query += " AND id != ?"
            params.append(exclude_queue_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            try:
                script = json.loads(row["script_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            payloads.append({"id": row["id"], "title": row["title"], "script": script})
        return payloads

    def recent_real_video_metrics(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT cq.id, cq.title, cq.niche, m.views, m.likes, m.comments, m.fetched_at
                FROM content_queue cq
                JOIN (
                    SELECT queue_id, MAX(fetched_at) AS latest_fetched_at
                    FROM metrics
                    GROUP BY queue_id
                ) latest ON latest.queue_id = cq.id
                JOIN metrics m
                    ON m.queue_id = latest.queue_id
                    AND m.fetched_at = latest.latest_fetched_at
                WHERE cq.status = ?
                ORDER BY cq.published_at DESC
                LIMIT ?
                """,
                (QueueStatus.PUBLISHED, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def approve(self, queue_id: int) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE content_queue
                SET status = ?, approved_at = ?, updated_at = ?, error = NULL
                WHERE id = ? AND status = ?
                """,
                (QueueStatus.APPROVED, now, now, queue_id, QueueStatus.PENDING_APPROVAL),
            )
            self._conn.commit()
            item = self.get_queue_item(queue_id)
            if item is None:
                raise KeyError(f"Queue item not found: {queue_id}")
            return item

    def approve_for_live_upload(self, queue_id: int) -> dict[str, Any]:
        now = utc_now_iso()
        self.update_queue_item(
            queue_id,
            approved_for_live_upload=1,
            live_approved_at=now,
            error=None,
        )
        item = self.get_queue_item(queue_id)
        if item is None:
            raise KeyError(f"Queue item not found: {queue_id}")
        return item

    def update_queue_item(self, queue_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(queue_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE content_queue SET {columns} WHERE id = ?",
                values,
            )
            self._conn.commit()

    def mark_failed(self, queue_id: int, error: str) -> None:
        self.update_queue_item(queue_id, status=QueueStatus.FAILED, error=error[-2000:])

    def increment_upload_attempt(self, queue_id: int) -> int:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE content_queue
                SET upload_attempt_count = COALESCE(upload_attempt_count, 0) + 1,
                    last_upload_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, queue_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT COALESCE(upload_attempt_count, 0) AS count FROM content_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
            return int(row["count"]) if row else 0

    def mark_upload_failure(self, queue_id: int, error: str, terminal: bool = False) -> None:
        fields: dict[str, Any] = {
            "last_upload_error": error[-2000:],
            "error": error[-2000:],
            "upload_blocked_reason": error[-2000:],
        }
        if terminal:
            fields["status"] = QueueStatus.FAILED
        self.update_queue_item(queue_id, **fields)

    def mark_needs_research(self, queue_id: int, reason: str, research: dict[str, Any]) -> None:
        self.update_queue_item(
            queue_id,
            status=QueueStatus.NEEDS_RESEARCH,
            error=reason[-2000:],
            upload_blocked_reason=reason[-2000:],
            research_json=json.dumps(research, ensure_ascii=True, sort_keys=True),
        )

    def mark_needs_fresh_source(self, queue_id: int, reason: str, research: dict[str, Any]) -> None:
        self.update_queue_item(
            queue_id,
            status=QueueStatus.NEEDS_FRESH_SOURCE,
            error=reason[-2000:],
            upload_blocked_reason=reason[-2000:],
            research_json=json.dumps(research, ensure_ascii=True, sort_keys=True),
            quality_score=float(research.get("freshness_score", 0.0)),
        )

    def mark_needs_trusted_source(self, queue_id: int, reason: str, research: dict[str, Any]) -> None:
        self.update_queue_item(
            queue_id,
            status=QueueStatus.NEEDS_TRUSTED_SOURCE,
            error=reason[-2000:],
            upload_blocked_reason=reason[-2000:],
            research_json=json.dumps(research, ensure_ascii=True, sort_keys=True),
            quality_score=float(research.get("trust_score", 0.0)),
        )

    def mark_safety_blocked(self, queue_id: int, reason: str, decision: dict[str, Any]) -> None:
        self.update_queue_item(
            queue_id,
            status=QueueStatus.SAFETY_BLOCKED,
            error=reason[-2000:],
            upload_blocked_reason=reason[-2000:],
            safety_json=json.dumps(decision, ensure_ascii=True, sort_keys=True),
            quality_score=float(decision.get("quality_score", 0)),
        )

    def mark_published(self, queue_id: int, youtube_video_id: str, paper_mode: bool) -> None:
        self.update_queue_item(
            queue_id,
            status=QueueStatus.PAPER_PUBLISHED if paper_mode else QueueStatus.PUBLISHED,
            youtube_video_id=youtube_video_id,
            published_at=utc_now_iso(),
            last_upload_error=None,
            upload_blocked_reason=None,
        )

    def has_duplicate_real_upload(
        self,
        queue_id: int,
        topic_key: str,
        video_path: str | None,
        title: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            topic_duplicate = self._conn.execute(
                """
                SELECT id, youtube_video_id
                FROM content_queue
                WHERE id != ?
                  AND topic_key = ?
                  AND status = ?
                  AND youtube_video_id IS NOT NULL
                LIMIT 1
                """,
                (queue_id, topic_key, QueueStatus.PUBLISHED),
            ).fetchone()
            video_duplicate = None
            if video_path:
                video_duplicate = self._conn.execute(
                    """
                    SELECT id, youtube_video_id
                    FROM content_queue
                    WHERE id != ?
                      AND video_path = ?
                      AND status = ?
                      AND youtube_video_id IS NOT NULL
                    LIMIT 1
                    """,
                    (queue_id, video_path, QueueStatus.PUBLISHED),
                ).fetchone()
            title_duplicate = None
            if title:
                title_duplicate = self._conn.execute(
                    """
                    SELECT id, youtube_video_id, script_json, title
                    FROM content_queue
                    WHERE id != ?
                      AND status = ?
                      AND youtube_video_id IS NOT NULL
                    """,
                    (queue_id, QueueStatus.PUBLISHED),
                ).fetchall()
                title_key = title.strip().lower()
                title_duplicate = next(
                    (
                        row
                        for row in title_duplicate
                        if self._published_title(row).strip().lower() == title_key
                    ),
                    None,
                )
        return {
            "topic_duplicate": dict(topic_duplicate) if topic_duplicate else None,
            "video_duplicate": dict(video_duplicate) if video_duplicate else None,
            "title_duplicate": dict(title_duplicate) if title_duplicate else None,
        }

    def _published_title(self, row: sqlite3.Row) -> str:
        script_json = row["script_json"]
        if script_json:
            try:
                payload = json.loads(script_json)
                title = str(payload.get("title", "") or "")
                if title:
                    return title
            except (TypeError, json.JSONDecodeError):
                pass
        return str(row["title"] or "")

    def count_waiting_for_upload(self) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM content_queue
                WHERE status IN (?, ?)
                  AND youtube_video_id IS NULL
                """,
                (QueueStatus.APPROVED, QueueStatus.READY),
            ).fetchone()
            return int(row["count"])

    def last_uploaded_video(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM content_queue
                WHERE status = ? AND youtube_video_id IS NOT NULL
                ORDER BY published_at DESC
                LIMIT 1
                """,
                (QueueStatus.PUBLISHED,),
            ).fetchone()
            return self.row_to_dict(row)

    def background_category_performance(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    COALESCE(cq.background_category, 'unknown') AS background_category,
                    COUNT(*) AS video_count,
                    AVG(COALESCE(cq.quality_score, 0)) AS avg_quality_score,
                    AVG(COALESCE(m.views, 0)) AS avg_views,
                    AVG(COALESCE(m.likes, 0)) AS avg_likes,
                    AVG(COALESCE(m.comments, 0)) AS avg_comments
                FROM content_queue cq
                LEFT JOIN (
                    SELECT queue_id, MAX(fetched_at) AS latest_fetched_at
                    FROM metrics
                    GROUP BY queue_id
                ) latest ON latest.queue_id = cq.id
                LEFT JOIN metrics m
                    ON m.queue_id = latest.queue_id
                    AND m.fetched_at = latest.latest_fetched_at
                WHERE cq.background_category IS NOT NULL
                GROUP BY cq.background_category
                ORDER BY video_count DESC, avg_quality_score DESC
                """
            ).fetchall()
            return [
                {
                    "background_category": str(row["background_category"]),
                    "video_count": int(row["video_count"]),
                    "avg_quality_score": round(float(row["avg_quality_score"] or 0.0), 1),
                    "avg_views": round(float(row["avg_views"] or 0.0), 1),
                    "avg_likes": round(float(row["avg_likes"] or 0.0), 1),
                    "avg_comments": round(float(row["avg_comments"] or 0.0), 1),
                }
                for row in rows
            ]

    def record_background_library_event(self, filename: str, event_type: str, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO background_library_events(filename, event_type, error, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (filename, event_type, error[-2000:] if error else None, utc_now_iso()),
            )
            self._conn.commit()

    def background_library_event_summary(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    filename,
                    SUM(CASE WHEN event_type = 'selected' THEN 1 ELSE 0 END) AS selected_count,
                    SUM(CASE WHEN event_type = 'failure' THEN 1 ELSE 0 END) AS error_count,
                    MAX(CASE WHEN event_type = 'failure' THEN error END) AS last_error,
                    MAX(created_at) AS last_event_at
                FROM background_library_events
                GROUP BY filename
                ORDER BY filename
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def last_used_background(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT background_filename AS filename, updated_at
                FROM content_queue
                WHERE background_filename IS NOT NULL
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            return self.row_to_dict(row)

    def background_library_performance(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    cq.background_filename AS filename,
                    COUNT(*) AS video_count,
                    AVG(COALESCE(cq.quality_score, 0)) AS avg_quality_score,
                    AVG(CASE WHEN cq.status = 'published' AND m.queue_id IS NOT NULL THEN m.views END) AS avg_views,
                    AVG(CASE WHEN cq.status = 'published' AND m.queue_id IS NOT NULL THEN m.likes END) AS avg_likes,
                    AVG(CASE WHEN cq.status = 'published' AND m.queue_id IS NOT NULL THEN m.comments END) AS avg_comments,
                    AVG(
                        CASE
                            WHEN cq.status = 'published' AND m.queue_id IS NOT NULL AND m.views > 0
                            THEN CAST(m.likes AS REAL) / m.views
                        END
                    ) AS avg_like_rate,
                    SUM(
                        CASE
                            WHEN cq.status = 'published' AND m.queue_id IS NOT NULL THEN 1
                            ELSE 0
                        END
                    ) AS metric_video_count
                FROM content_queue cq
                LEFT JOIN (
                    SELECT queue_id, MAX(fetched_at) AS latest_fetched_at
                    FROM metrics
                    GROUP BY queue_id
                ) latest ON latest.queue_id = cq.id
                LEFT JOIN metrics m
                    ON m.queue_id = latest.queue_id
                    AND m.fetched_at = latest.latest_fetched_at
                WHERE cq.background_filename IS NOT NULL
                GROUP BY cq.background_filename
                ORDER BY video_count DESC, avg_quality_score DESC
                """
            ).fetchall()
            return [
                {
                    "filename": str(row["filename"]),
                    "video_count": int(row["video_count"]),
                    "avg_quality_score": round(float(row["avg_quality_score"] or 0.0), 1),
                    "avg_views": round(float(row["avg_views"] or 0.0), 1)
                    if row["avg_views"] is not None
                    else None,
                    "avg_likes": round(float(row["avg_likes"] or 0.0), 1)
                    if row["avg_likes"] is not None
                    else None,
                    "avg_comments": round(float(row["avg_comments"] or 0.0), 1)
                    if row["avg_comments"] is not None
                    else None,
                    "avg_like_rate": round(float(row["avg_like_rate"] or 0.0), 4)
                    if row["avg_like_rate"] is not None
                    else None,
                    "youtube_stats_available": int(row["metric_video_count"] or 0) > 0,
                }
                for row in rows
            ]

    def last_scheduler_failure(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM youtube_scheduler_events
                WHERE status IN ('failure', 'validation_failed', 'stopped', 'paused')
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            return self.row_to_dict(row)

    def record_scheduler_event(
        self,
        event_type: str,
        status: str,
        reason: str = "",
        queue_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO youtube_scheduler_events(event_type, status, queue_id, reason, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    status,
                    queue_id,
                    reason[-2000:] if reason else "",
                    json.dumps(payload or {}, ensure_ascii=True, sort_keys=True),
                    now,
                ),
            )
            self._conn.commit()

    def scheduler_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM youtube_scheduler_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def scheduler_event_counts_today(self) -> dict[str, int]:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM youtube_scheduler_events
                WHERE created_at >= ?
                GROUP BY status
                """,
                (start.isoformat(),),
            ).fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def consecutive_upload_failures(self) -> int:
        events = self.scheduler_events(limit=20)
        count = 0
        for event in events:
            if event["event_type"] != "upload":
                continue
            if event["status"] == "success":
                break
            if event["status"] == "failure":
                count += 1
        return count

    def validation_failure_rate(self, limit: int = 10) -> dict[str, Any]:
        events = [
            event
            for event in self.scheduler_events(limit=limit)
            if event["event_type"] in {"validation", "upload_gate"}
        ]
        if not events:
            return {"total": 0, "failures": 0, "rate": 0.0}
        failures = sum(1 for event in events if event["status"] in {"validation_failed", "skipped"})
        return {"total": len(events), "failures": failures, "rate": failures / len(events)}

    def set_scheduler_state(self, key: str, value: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scheduler_state(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=True, sort_keys=True), now),
            )
            self._conn.commit()

    def get_scheduler_state(self, key: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM scheduler_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(str(row["value_json"]))
        except json.JSONDecodeError:
            return {}

    def youtube_quota_used_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(units), 0) AS units
                FROM youtube_quota_usage
                WHERE usage_date = ?
                """,
                (today,),
            ).fetchone()
            return int(row["units"])

    def record_youtube_quota_usage(self, queue_id: int, operation: str, units: int) -> None:
        now = utc_now_iso()
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO youtube_quota_usage(usage_date, queue_id, operation, units, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (today, queue_id, operation, int(units), now),
            )
            self._conn.commit()

    def get_niche_multiplier(self, niche: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT performance_score FROM niche_performance WHERE niche = ?",
                (niche,),
            ).fetchone()
            if row is None:
                return 1.0
            return max(0.75, min(1.75, float(row["performance_score"])))

    def record_metrics(
        self,
        queue_id: int,
        youtube_video_id: str,
        niche: str,
        views: int,
        likes: int,
        comments: int,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO metrics(queue_id, youtube_video_id, views, likes, comments, fetched_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (queue_id, youtube_video_id, views, likes, comments, now),
            )
            existing = self._conn.execute(
                "SELECT * FROM niche_performance WHERE niche = ?",
                (niche,),
            ).fetchone()
            if existing is None:
                performance_score = max(0.75, min(1.75, 1 + (views / 10_000) + (likes / 2_000)))
                self._conn.execute(
                    """
                    INSERT INTO niche_performance(
                        niche, published_count, avg_views, avg_likes, performance_score, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (niche, 1, views, likes, performance_score, now),
                )
            else:
                count = int(existing["published_count"]) + 1
                avg_views = ((float(existing["avg_views"]) * (count - 1)) + views) / count
                avg_likes = ((float(existing["avg_likes"]) * (count - 1)) + likes) / count
                performance_score = max(0.75, min(1.75, 1 + (avg_views / 10_000) + (avg_likes / 2_000)))
                self._conn.execute(
                    """
                    UPDATE niche_performance
                    SET published_count = ?, avg_views = ?, avg_likes = ?,
                        performance_score = ?, updated_at = ?
                    WHERE niche = ?
                    """,
                    (count, avg_views, avg_likes, performance_score, now, niche),
                )
            self._conn.commit()
