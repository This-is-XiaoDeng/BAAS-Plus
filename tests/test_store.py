"""SQLite 状态存储测试"""
from baas_plus.activity import EventType, GameEvent
from baas_plus.store import Store


def test_activity_seen_flow(tmp_path):
    store = Store(tmp_path / "test.db")
    event = GameEvent(id=42, title="新活动", start_at=100, end_at=200, event_type=EventType.EVENT)
    assert not store.is_activity_seen(event)
    store.mark_activity_seen(event)
    assert store.is_activity_seen(event)
    rows = store.list_activities()
    assert rows[0]["title"] == "新活动"
    assert rows[0]["pushed"] == 0


def test_mark_pushed(tmp_path):
    store = Store(tmp_path / "test.db")
    event = GameEvent(id=7, title="活动A", start_at=100, end_at=200, event_type=EventType.EVENT)
    store.mark_activity_seen(event, pushed=True)
    rows = store.list_activities()
    assert rows[0]["pushed"] == 1
    assert rows[0]["pushed_at"] is not None


def test_record_flow(tmp_path):
    store = Store(tmp_path / "test.db")
    rid = store.add_record("running", "开始")
    store.finish_record(rid, "success", "完成", {"tasks": ["cafe_reward"]})
    records = store.list_records()
    assert len(records) == 1
    assert records[0]["status"] == "success"
    assert records[0]["detail"]["tasks"] == ["cafe_reward"]


def test_mark_activity_idempotent(tmp_path):
    store = Store(tmp_path / "test.db")
    e1 = GameEvent(id=1, title="t", start_at=0, end_at=10, event_type=EventType.EVENT)
    store.mark_activity_seen(e1, pushed=True)
    e2 = GameEvent(id=1, title="t", start_at=0, end_at=10, event_type=EventType.EVENT)
    store.mark_activity_seen(e2, pushed=False)  # 不降级
    rows = store.list_activities()
    assert rows[0]["pushed"] == 1


def test_reset_pushed_all_and_single(tmp_path):
    """reset_pushed：全部重置 / 按 key 重置，pushed 标记清零"""
    from baas_plus.activity import EventType, GameEvent
    from baas_plus.store import Store

    store = Store(tmp_path / "s.db")
    e1 = GameEvent(id=1, title="A", start_at=100, end_at=200, event_type=EventType.EVENT)
    e2 = GameEvent(id=2, title="B", start_at=100, end_at=200, event_type=EventType.EVENT)
    store.mark_activity_seen(e1, pushed=True)
    store.mark_activity_seen(e2, pushed=True)
    assert all(r["pushed"] for r in store.list_activities())

    # 按 key 重置
    n = store.reset_pushed(e1.key)
    assert n == 1
    rows = {r["event_key"]: r["pushed"] for r in store.list_activities()}
    assert rows[e1.key] == 0 and rows[e2.key] == 1

    # 全部重置（SQLite rowcount 统计匹配行数，同值更新也计入）
    n = store.reset_pushed()
    assert n == 2
    assert all(r["pushed"] == 0 for r in store.list_activities())
