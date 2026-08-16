"""本地状态存储：SQLite

- activities: 活动推送状态（哪些活动已推送/已推图，避免重复触发），按账号隔离
- records:   执行记录（WebUI 展示），按账号隔离

多账号维度：activities 主键为 (event_key, account)，records 带 account 列。
旧版单账号库（无 account 维度）在打开时自动迁移，旧数据归入 acc_default。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .activity import GameEvent

logger = logging.getLogger(__name__)


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
                    event_key   TEXT NOT NULL,
                    account     TEXT NOT NULL,
                    event_id    INTEGER NOT NULL,
                    event_type  TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    start_at    INTEGER NOT NULL,
                    end_at      INTEGER NOT NULL,
                    seen_at     INTEGER NOT NULL,
                    pushed      INTEGER NOT NULL DEFAULT 0,
                    pushed_at   INTEGER,
                    PRIMARY KEY (event_key, account)
                );
                CREATE TABLE IF NOT EXISTS records (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at  INTEGER NOT NULL,
                    finished_at INTEGER,
                    status      TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    detail      TEXT NOT NULL DEFAULT '{}',
                    account     TEXT NOT NULL DEFAULT 'acc_default'
                );
                """
            )
            self._migrate_schema()
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """旧版单账号库自动升级（幂等，仅缺 account 维度时执行）：

        - activities 无 account 列（旧主键 event_key）→ 重建为复合主键
          (event_key, account)，旧数据归入 acc_default；
        - records 无 account 列 → 加列（默认 acc_default）。
        """
        with self._lock:
            a_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(activities)")}
            if "account" not in a_cols:
                self._conn.executescript(
                    """
                    ALTER TABLE activities RENAME TO activities_old;
                    CREATE TABLE activities (
                        event_key   TEXT NOT NULL,
                        account     TEXT NOT NULL,
                        event_id    INTEGER NOT NULL,
                        event_type  TEXT NOT NULL,
                        title       TEXT NOT NULL,
                        start_at    INTEGER NOT NULL,
                        end_at      INTEGER NOT NULL,
                        seen_at     INTEGER NOT NULL,
                        pushed      INTEGER NOT NULL DEFAULT 0,
                        pushed_at   INTEGER,
                        PRIMARY KEY (event_key, account)
                    );
                    INSERT INTO activities (event_key, account, event_id, event_type, title, start_at, end_at, seen_at, pushed, pushed_at)
                        SELECT event_key, 'acc_default', event_id, event_type, title, start_at, end_at, seen_at, pushed, pushed_at
                        FROM activities_old;
                    DROP TABLE activities_old;
                    """
                )
                logger.info("已迁移 activities 表：新增 account 维度（旧数据归入 acc_default）")
            r_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(records)")}
            if "account" not in r_cols:
                self._conn.execute(
                    "ALTER TABLE records ADD COLUMN account TEXT NOT NULL DEFAULT 'acc_default'"
                )
                logger.info("已迁移 records 表：新增 account 列（旧数据归入 acc_default）")

    # ---- 活动状态 ----

    def is_activity_seen(self, account: str, event: GameEvent) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM activities WHERE event_key = ? AND account = ?",
                (event.key, account),
            ).fetchone()
        return row is not None

    def mark_activity_seen(self, account: str, event: GameEvent, pushed: bool = False) -> None:
        """记录活动已处理（pushed=True 表示已触发推图/已推送通知）；按账号记录"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activities (event_key, account, event_id, event_type, title, start_at, end_at, seen_at, pushed, pushed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key, account) DO UPDATE SET
                    title = excluded.title,
                    start_at = excluded.start_at,
                    end_at = excluded.end_at,
                    event_type = excluded.event_type,
                    pushed = MAX(activities.pushed, excluded.pushed),
                    pushed_at = COALESCE(activities.pushed_at, excluded.pushed_at)
                """,
                (
                    event.key,
                    account,
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

    def list_activities(self, account: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activities WHERE account = ? ORDER BY start_at DESC LIMIT ?",
                (account, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def reset_pushed(self, account: str, event_key: str | None = None) -> int:
        """重置指定账号的活动推送/推图标记（event_key 为空时重置该账号全部），返回受影响行数"""
        with self._lock:
            if event_key:
                cur = self._conn.execute(
                    "UPDATE activities SET pushed = 0, pushed_at = NULL WHERE account = ? AND event_key = ?",
                    (account, event_key),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE activities SET pushed = 0, pushed_at = NULL WHERE account = ?",
                    (account,),
                )
            self._conn.commit()
            return cur.rowcount

    # ---- 执行记录 ----

    def add_record(self, account: str, status: str, summary: str, detail: dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO records (started_at, status, summary, detail, account) VALUES (?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    status,
                    summary,
                    json.dumps(detail or {}, ensure_ascii=False),
                    account,
                ),
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

    def list_records(self, account: str | None = None, limit: int = 50) -> list[dict]:
        """执行记录（account=None 时返回全部账号）"""
        with self._lock:
            if account:
                rows = self._conn.execute(
                    "SELECT * FROM records WHERE account = ? ORDER BY id DESC LIMIT ?",
                    (account, limit),
                ).fetchall()
            else:
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
