"""SQLite 状态存储测试（多账号维度）"""
import sqlite3

from baas_plus.activity import EventType, GameEvent
from baas_plus.store import Store

ACC_A = "acc_a"
ACC_B = "acc_b"


def test_activity_seen_flow(tmp_path):
    store = Store(tmp_path / "test.db")
    event = GameEvent(id=42, title="新活动", start_at=100, end_at=200, event_type=EventType.EVENT)
    assert not store.is_activity_seen(ACC_A, event)
    store.mark_activity_seen(ACC_A, event)
    assert store.is_activity_seen(ACC_A, event)
    rows = store.list_activities(ACC_A)
    assert rows[0]["title"] == "新活动"
    assert rows[0]["pushed"] == 0


def test_mark_pushed(tmp_path):
    store = Store(tmp_path / "test.db")
    event = GameEvent(id=7, title="活动A", start_at=100, end_at=200, event_type=EventType.EVENT)
    store.mark_activity_seen(ACC_A, event, pushed=True)
    rows = store.list_activities(ACC_A)
    assert rows[0]["pushed"] == 1
    assert rows[0]["pushed_at"] is not None


def test_record_flow(tmp_path):
    store = Store(tmp_path / "test.db")
    rid = store.add_record(ACC_A, "running", "开始")
    store.finish_record(rid, "success", "完成", {"tasks": ["cafe_reward"]})
    records = store.list_records(ACC_A)
    assert len(records) == 1
    assert records[0]["status"] == "success"
    assert records[0]["detail"]["tasks"] == ["cafe_reward"]
    assert records[0]["account"] == ACC_A


def test_mark_activity_idempotent(tmp_path):
    store = Store(tmp_path / "test.db")
    e1 = GameEvent(id=1, title="t", start_at=0, end_at=10, event_type=EventType.EVENT)
    store.mark_activity_seen(ACC_A, e1, pushed=True)
    e2 = GameEvent(id=1, title="t", start_at=0, end_at=10, event_type=EventType.EVENT)
    store.mark_activity_seen(ACC_A, e2, pushed=False)  # 不降级
    rows = store.list_activities(ACC_A)
    assert rows[0]["pushed"] == 1


def test_reset_pushed_all_and_single(tmp_path):
    """reset_pushed：该账号全部重置 / 按 key 重置，pushed 标记清零"""
    store = Store(tmp_path / "s.db")
    e1 = GameEvent(id=1, title="A", start_at=100, end_at=200, event_type=EventType.EVENT)
    e2 = GameEvent(id=2, title="B", start_at=100, end_at=200, event_type=EventType.EVENT)
    store.mark_activity_seen(ACC_A, e1, pushed=True)
    store.mark_activity_seen(ACC_A, e2, pushed=True)
    assert all(r["pushed"] for r in store.list_activities(ACC_A))

    # 按 key 重置
    n = store.reset_pushed(ACC_A, e1.key)
    assert n == 1
    rows = {r["event_key"]: r["pushed"] for r in store.list_activities(ACC_A)}
    assert rows[e1.key] == 0 and rows[e2.key] == 1

    # 该账号全部重置（SQLite rowcount 统计匹配行数，同值更新也计入）
    n = store.reset_pushed(ACC_A)
    assert n == 2
    assert all(r["pushed"] == 0 for r in store.list_activities(ACC_A))


# ---- 多账号隔离 ----


def test_activity_isolation_between_accounts(tmp_path):
    """同一活动在不同账号互不影响：A 推图后 B 仍视为新"""
    store = Store(tmp_path / "iso.db")
    event = GameEvent(id=1, title="活动", start_at=100, end_at=200, event_type=EventType.EVENT)
    store.mark_activity_seen(ACC_A, event, pushed=True)
    assert store.is_activity_seen(ACC_A, event)
    assert not store.is_activity_seen(ACC_B, event)  # B 账号未见过 → 会再触发推图
    store.mark_activity_seen(ACC_B, event)
    rows_a = store.list_activities(ACC_A)
    rows_b = store.list_activities(ACC_B)
    assert rows_a[0]["pushed"] == 1 and rows_b[0]["pushed"] == 0
    # 各自独立主键：同 event_key 两条记录
    assert len(store.list_activities(ACC_A, limit=500)) == 1
    assert len(store.list_activities(ACC_B, limit=500)) == 1


def test_reset_pushed_scoped_to_account(tmp_path):
    """重置只影响指定账号，不误伤其他账号"""
    store = Store(tmp_path / "scoped.db")
    e = GameEvent(id=1, title="A", start_at=100, end_at=200, event_type=EventType.EVENT)
    store.mark_activity_seen(ACC_A, e, pushed=True)
    store.mark_activity_seen(ACC_B, e, pushed=True)
    store.reset_pushed(ACC_A)
    assert not store.list_activities(ACC_A)[0]["pushed"]
    assert store.list_activities(ACC_B)[0]["pushed"] == 1


def test_records_filter_by_account(tmp_path):
    store = Store(tmp_path / "rec.db")
    store.add_record(ACC_A, "running", "A 开始")
    store.add_record(ACC_B, "running", "B 开始")
    assert len(store.list_records(ACC_A)) == 1
    assert store.list_records(ACC_A)[0]["account"] == ACC_A
    assert len(store.list_records(account=None)) == 2  # 全部


# ---- 旧库自动迁移 ----


def _create_legacy_db(path):
    """构造旧版单账号 schema（activities 无 account 列，records 无 account 列）"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE activities (
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
        CREATE TABLE records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  INTEGER NOT NULL,
            finished_at INTEGER,
            status      TEXT NOT NULL,
            summary     TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO activities (event_key, event_id, event_type, title, start_at, end_at, seen_at, pushed)
        VALUES ('event:1', 1, 'event', '旧活动', 100, 200, 100, 1);
        INSERT INTO records (started_at, status, summary) VALUES (100, 'success', '旧记录');
        """
    )
    conn.commit()
    conn.close()


def test_legacy_db_auto_migration(tmp_path):
    """旧版单账号库打开时自动升级：数据归入 acc_default，按账号可查"""
    db = tmp_path / "legacy.db"
    _create_legacy_db(db)
    store = Store(db)
    # activities 迁移：复合主键 + 旧数据归 acc_default
    assert store.is_activity_seen("acc_default", GameEvent(id=1, title="旧活动", start_at=100, end_at=200, event_type=EventType.EVENT))
    rows = store.list_activities("acc_default")
    assert rows[0]["pushed"] == 1
    assert len(store.list_activities("acc_other")) == 0
    # records 迁移：加 account 列，旧记录归 acc_default
    records = store.list_records()
    assert len(records) == 1
    assert records[0]["account"] == "acc_default"
    assert records[0]["status"] == "success"


def test_migration_idempotent(tmp_path):
    """已迁移的库再次打开不重复迁移"""
    db = tmp_path / "idem.db"
    store = Store(db)
    store.mark_activity_seen(ACC_A, GameEvent(id=1, title="x", start_at=0, end_at=10, event_type=EventType.EVENT))
    store2 = Store(db)  # 再打开
    assert store2.is_activity_seen(ACC_A, GameEvent(id=1, title="x", start_at=0, end_at=10, event_type=EventType.EVENT))
