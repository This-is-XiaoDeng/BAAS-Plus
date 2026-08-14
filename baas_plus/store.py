"""本地状态存储：SQLite

- activities: 活动推送状态（哪些活动已推送/已推图，避免重复触发）
- records:   执行记录（WebUI 展示）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .activity import GameEvent


class Store:
    """SQLite 状态存储（线程安全：FastAPI 同步端点在线程池中调用）"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    event_key   TEXT PRIMARY KEY,
                    event_id    INTEGER NOT NULL,
                    event_type  TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    start_at    INTEGER NOT NULL,
                    end_at      INTEGER NOT NULL,
                    seen_at     INTEGER NOT NULL,
                    pushed      INTEGER NOT NULL DEFAULT 0,
                    pushed_at   INTEGER
                );
                CREATE TABLE IF NOT EXISTS records (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at  INTEGER NOT NULL,
                    finished_at INTEGER,
                    status      TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    detail      TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            self._conn.commit()

    # ---- 活动状态 ----

    def is_activity_seen(self, event: GameEvent) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM activities WHERE event_key = ?", (event.key,)
            ).fetchone()
        return row is not None

    def mark_activity_seen(self, event: GameEvent, pushed: bool = False) -> None:
        """记录活动已处理（pushed=True 表示已触发推图/已推送通知）"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activities (event_key, event_id, event_type, title, start_at, end_at, seen_at, pushed, pushed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    title = excluded.title,
                    start_at = excluded.start_at,
                    end_at = excluded.end_at,
                    pushed = MAX(activities.pushed, excluded.pushed),
                    pushed_at = COALESCE(activities.pushed_at, excluded.pushed_at)
                """,
                (
                    event.key,
                    event.id,
                    event.event_type.value,
                    event.title,
                    event.start_at,
                    event.end_at,
                    int(time.time()),
                    1 if pushed else 0,
                    int(time.time()) if pushed else None,
                ),
            )
            self._conn.commit()

    def list_activities(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activities ORDER BY start_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 执行记录 ----

    def add_record(self, status: str, summary: str, detail: dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO records (started_at, status, summary, detail) VALUES (?, ?, ?, ?)",
                (int(time.time()), status, summary, json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def finish_record(self, record_id: int, status: str, summary: str, detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE records SET finished_at = ?, status = ?, summary = ?, detail = ? WHERE id = ?",
                (int(time.time()), status, summary, json.dumps(detail or {}, ensure_ascii=False), record_id),
            )
            self._conn.commit()

    def list_records(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.get("detail") or "{}")
            result.append(d)
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()
