"""核心引擎测试（mock bridge / fetcher，不依赖真实 BAAS 与网络）"""
import time

import pytest

from baas_plus.activity import EventType, GameEvent
from baas_plus.baas_bridge import compute_sweep_times
from baas_plus.config import AppConfig
from baas_plus.engine import Engine, HARD_MAX_TIMES


class FakeBridge:
    """模拟 BaasBridge：记录调用，返回可配置结果"""

    def __init__(self, ap=120):
        self.ap = ap
        self.started = False
        self.solves: list[str] = []
        self.current_activity = None
        self.normal_tasks = None
        self.hard_tasks = None
        self.activity_sweep = None
        self._next_time = 0
        self.baas_sweep_config = {"mainlinePriority": "", "hardPriority": ""}

    def start_simulator(self):
        self.started = True
        return "127.0.0.1:16384"

    def create_baas(self, adb_address=None):
        assert adb_address == "127.0.0.1:16384"

    def solve(self, task):
        self.solves.append(task)
        return True

    @property
    def last_next_time(self):
        return self._next_time

    def get_ap(self):
        return self.ap

    def get_baas_sweep_config(self):
        return self.baas_sweep_config

    def set_current_activity(self, module_name):
        self.current_activity = module_name

    def set_sweep_tasks(self, normal_tasks, hard_tasks):
        self.normal_tasks = normal_tasks
        self.hard_tasks = hard_tasks

    def set_activity_sweep(self, task_number, times):
        self.activity_sweep = (task_number, times)

    def stop(self):
        pass


class FakeFetcher:
    def __init__(self, events=None):
        self.events = events or []

    async def fetch_all(self):
        return self.events


def make_engine(bridge=None, events=None, data_dir=None, **cfg_kwargs):
    if data_dir is None:
        import tempfile

        data_dir = tempfile.mkdtemp(prefix="baas_plus_test_")
    config = AppConfig(data_dir=data_dir, **cfg_kwargs)
    config.baas.current_activity = "SayBing"
    engine = Engine(config, bridge=bridge or FakeBridge(), fetcher=FakeFetcher(events))
    return engine


def test_compute_sweep_times():
    assert compute_sweep_times(120, 10, 20) == 12
    assert compute_sweep_times(120, 20, 3) == 3  # 上限 3（困难图）
    assert compute_sweep_times(5, 10, 20) == 0  # 不足一次
    assert compute_sweep_times(0, 10, 20) == 0


@pytest.mark.asyncio
async def test_run_once_no_activity(tmp_path):
    bridge = FakeBridge(ap=100)
    engine = make_engine(
        bridge, events=[], data_dir=str(tmp_path),
        baas={"tasks": ["cafe_reward", "mail"]},
        sweep={"normal_tasks": ["15-1-99"]},
    )
    result = await engine.run_once()
    assert bridge.started
    assert "cafe_reward" in bridge.solves
    assert "mail" in bridge.solves
    # 无活动：扫普通图，auto 模式按体力 100/10=10 次重算
    assert bridge.normal_tasks == ["15-1-10"]
    assert result.status == "success"
    assert result.ap_before_sweep == 100


@pytest.mark.asyncio
async def test_run_once_new_activity_triggers_push(tmp_path):
    event = GameEvent(
        id=1, title="新活动", start_at=int(time.time()) - 10,
        end_at=int(time.time()) + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=200)
    engine = make_engine(bridge, events=[event], data_dir=str(tmp_path))
    result = await engine.run_once()
    assert bridge.current_activity == "SayBing"
    assert "explore_activity_story" in bridge.solves
    assert result.pushed_activities == ["explore_activity_story"]
    # 活动优先：activity_sweep 被设置且执行
    assert bridge.activity_sweep == ("1", "-1")
    assert "activity_sweep" in bridge.solves
    assert result.status == "success"


@pytest.mark.asyncio
async def test_second_run_does_not_repeat_push(tmp_path):
    """同一活动第二次运行时不再触发推图"""
    event = GameEvent(
        id=1, title="新活动", start_at=int(time.time()) - 10,
        end_at=int(time.time()) + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=100)
    engine = make_engine(bridge, events=[event], data_dir=str(tmp_path))
    await engine.run_once()
    assert "explore_activity_story" in bridge.solves

    bridge2 = FakeBridge(ap=100)
    engine2 = make_engine(bridge2, events=[event], data_dir=str(tmp_path))
    await engine2.run_once()
    assert "explore_activity_story" not in bridge2.solves
    assert bridge2.current_activity is None


@pytest.mark.asyncio
async def test_fixed_strategy_times(tmp_path):
    config_kwargs = {"sweep": {"strategy": "fixed", "fixed_times": 5, "normal_tasks": ["15-1-3"]}}
    bridge = FakeBridge(ap=1000)
    engine = make_engine(bridge, events=[], data_dir=str(tmp_path), **config_kwargs)
    await engine.run_once()
    assert bridge.normal_tasks == ["15-1-3"]  # fixed 模式不改次数


@pytest.mark.asyncio
async def test_hard_sweep_max_cap(tmp_path):
    bridge = FakeBridge(ap=500)
    engine = make_engine(bridge, events=[], data_dir=str(tmp_path))
    engine.config.sweep.hard_tasks = ["20-1-max"]
    await engine.run_once()
    assert bridge.hard_tasks == [f"20-1-{HARD_MAX_TIMES}"]  # auto 模式下 max → 上限 3


@pytest.mark.asyncio
async def test_task_failure_marks_partial(tmp_path):
    class BrokenBridge(FakeBridge):
        def solve(self, task):
            if task == "mail":
                raise RuntimeError("模拟失败")
            return super().solve(task)

    bridge = BrokenBridge(ap=100)
    engine = make_engine(bridge, events=[], data_dir=str(tmp_path))
    result = await engine.run_once()
    assert result.status == "partial"
    assert "cafe_reward" in result.executed_tasks
    assert "mail" not in result.executed_tasks


@pytest.mark.asyncio
async def test_run_failure_when_simulator_fails(tmp_path):
    class FailBridge(FakeBridge):
        def start_simulator(self):
            raise RuntimeError("模拟器启动失败")

    bridge = FailBridge()
    engine = make_engine(bridge, events=[], data_dir=str(tmp_path))
    result = await engine.run_once()
    assert result.status == "failed"
    assert "模拟器启动失败" in result.summary


def test_has_active_activity(tmp_path):
    engine = make_engine(FakeBridge(), events=[], data_dir=str(tmp_path))
    assert not engine.has_active_activity()  # 空 store
    from baas_plus.store import Store

    store = Store(engine.config.data_path / "x.db")
    store.mark_activity_seen(
        GameEvent(id=9, title="A", start_at=0, end_at=int(time.time()) + 1000, event_type=EventType.EVENT)
    )
    engine.store = store
    assert engine.has_active_activity()


def test_has_active_activity_excludes_assault(tmp_path):
    """总力战/大决战等 assault 事件不应触发「有活动优先扫活动」"""
    from baas_plus.store import Store

    engine = make_engine(FakeBridge(), events=[], data_dir=str(tmp_path))
    store = Store(engine.config.data_path / "x.db")
    store.mark_activity_seen(
        GameEvent(id=10, title="总力战", start_at=0, end_at=int(time.time()) + 1000, event_type=EventType.ASSAULT)
    )
    store.mark_activity_seen(
        GameEvent(id=11, title="卡池", start_at=0, end_at=int(time.time()) + 1000, event_type=EventType.CARD)
    )
    engine.store = store
    assert not engine.has_active_activity()


@pytest.mark.asyncio
async def test_sweep_tasks_skipped_in_task_phase(tmp_path):
    """勾选任务里的扫荡类任务（normal_task/activity_sweep）不重复执行，统一由扫荡阶段调度"""
    bridge = FakeBridge(ap=200)
    engine = make_engine(
        bridge,
        events=[],
        data_dir=str(tmp_path),
        baas={"tasks": ["cafe_reward", "normal_task", "activity_sweep"]},
        sweep={"normal_tasks": ["15-1-99"]},
    )
    result = await engine.run_once()
    # 任务阶段只执行非扫荡任务；扫荡类在扫荡阶段按体力执行
    assert bridge.solves.count("cafe_reward") == 1
    assert bridge.solves.count("normal_task") == 1  # 仅扫荡阶段一次
    assert "activity_sweep" not in bridge.solves  # 无活动时不执行活动扫荡
    assert result.executed_tasks == ["restart", "cafe_reward"]


@pytest.mark.asyncio
async def test_arena_loops_until_tickets_done(tmp_path, monkeypatch):
    """arena 自动重复：next_time>0 时等待冷却后继续，直到票用完（next_time=0）"""
    monkeypatch.setattr("time.sleep", lambda s: None)  # 测试中不真等冷却
    next_times = iter([55, 55, 0])  # 第 1、2 场后冷却，第 3 场结束

    class ArenaBridge(FakeBridge):
        @property
        def last_next_time(self):
            try:
                return next(next_times)
            except StopIteration:
                return 0

    bridge = ArenaBridge(ap=100)
    engine = make_engine(
        bridge,
        events=[],
        data_dir=str(tmp_path),
        baas={"tasks": ["arena"]},
        sweep={"normal_tasks": []},
    )
    result = await engine.run_once()
    assert bridge.solves.count("arena") == 3
    assert result.executed_tasks.count("arena") == 3


@pytest.mark.asyncio
async def test_sweep_fallback_to_baas_config(tmp_path):
    """BAAS-Plus 扫荡列表为空时，回退读取 BAAS 配置里的 mainlinePriority/hardPriority"""
    bridge = FakeBridge(ap=120)
    bridge.baas_sweep_config = {"mainlinePriority": "5-1-3,6-1-2", "hardPriority": "20-1-1"}
    engine = make_engine(
        bridge,
        events=[],
        data_dir=str(tmp_path),
        baas={"tasks": ["mail"]},
        sweep={"normal_tasks": [], "hard_tasks": []},
    )
    result = await engine.run_once()
    # auto 模式按体力重算：120/10=12 次（上限 99），原次数被重算
    assert bridge.normal_tasks == ["5-1-12", "6-1-12"]
    assert bridge.hard_tasks == ["20-1-3"]  # 困难图按体力重算，封顶 3 次
    assert result.status == "success"
