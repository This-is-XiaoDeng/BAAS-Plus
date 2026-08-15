"""核心引擎测试（mock bridge / fetcher，不依赖真实 BAAS 与网络）"""
import time

import pytest

from baas_plus.activity import (
    ACTIVITY_MODULE_ALIASES,
    EventType,
    GameEvent,
)
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
        self.banner_text = ""
        self.banner_module: str | None = None

    def start_simulator(self):
        self.started = True
        return "127.0.0.1:16384"

    def create_baas(self, adb_address=None):
        assert adb_address == "127.0.0.1:16384"

    def launch_game(self):
        self.solves.append("restart")
        return True

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

    def list_activity_modules(self):
        return getattr(self, "activity_modules", [])

    def activity_module_available(self, module_name):
        # 未设置白名单时默认放行（测试兼容）；设置后按白名单校验
        mods = getattr(self, "activity_modules", None)
        if mods is None:
            return True
        return module_name in mods

    def ocr_banner(self):
        # 支持列表模拟轮播图换页（每次 OCR 取下一页文本）
        if isinstance(self.banner_text, list):
            return self.banner_text.pop(0) if self.banner_text else ""
        return self.banner_text

    def match_banner_activity(self, candidates):
        """模拟模板匹配

        默认命中候选第一个（测试关注点不在轮播图等待时直接通过）；
        banner_module=False 表示永不命中（换页等待/OCR 兜底测试用）；
        banner_module=<模块名> 时仅在候选中才命中。
        """
        if not candidates:
            return None
        if self.banner_module is False:
            return None
        if self.banner_module:
            return self.banner_module if self.banner_module in candidates else None
        return candidates[0]

    def click_banner_enter(self):
        self.solves.append("click_banner_enter")
        return True

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
    # 默认给一个活动模块便于推图测试；显式传 current_activity 可覆盖（如 ""）
    config.baas.current_activity = (cfg_kwargs.get("baas") or {}).get(
        "current_activity", "SayBing"
    )
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
    # 第二次运行：进行中活动 → 活动扫荡阶段设置了模块（不重复推图）
    assert bridge2.current_activity == "SayBing"


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
async def test_not_started_activity_not_pushed(tmp_path):
    """未开始的活动不推送、不标记已见（等正式开始后下一次执行再检测）"""
    from baas_plus.store import Store

    now = int(time.time())
    not_started = GameEvent(
        id=21, title="预告活动", start_at=now + 3600, end_at=now + 86400, event_type=EventType.EVENT
    )
    engine = make_engine(FakeBridge(), events=[not_started], data_dir=str(tmp_path))
    result = await engine.run_once()
    # 未开始：不推图、不算新活动
    assert result.new_activities == []
    assert result.pushed_activities == []
    # 未开始：不标记已见 → 活动开始时再跑一次会被检测为新活动
    store = Store(engine.config.data_path / "baas_plus.db")
    assert not store.is_activity_seen(not_started)

    started = GameEvent(
        id=21, title="预告活动", start_at=now - 10, end_at=now + 86400, event_type=EventType.EVENT
    )
    engine2 = make_engine(FakeBridge(), events=[started], data_dir=str(tmp_path))
    result2 = await engine2.run_once()
    assert len(result2.new_activities) == 1
    assert "explore_activity_story" in engine2.bridge.solves


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
    engine.config.baas.current_activity = ""  # 无手动配置 + 无进行中活动 → 不扫活动
    result = await engine.run_once()
    # 任务阶段只执行非扫荡任务；扫荡类在扫荡阶段按体力执行
    assert bridge.solves.count("cafe_reward") == 1
    assert bridge.solves.count("normal_task") == 1  # 仅扫荡阶段一次
    assert "activity_sweep" not in bridge.solves  # 无活动时不执行活动扫荡
    assert result.executed_tasks == ["restart", "cafe_reward"]


@pytest.mark.asyncio
async def test_activity_sweep_selects_running_activity(tmp_path):
    """活动扫荡选择进行中的活动模块，不扫已结束（仅兑换可用）的旧活动"""
    now = int(time.time())
    running = GameEvent(
        id=31, title="【复刻活动】「CODE：BOX」", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["CodeBox", "HighlanderRailroadExplosionIncident"]
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    # 标题含英文关键词 CODE/BOX → 匹配 CodeBox，而非 BAAS 默认的旧活动模块
    assert bridge.current_activity == "CodeBox"
    assert bridge.solves.count("activity_sweep") == 1


@pytest.mark.asyncio
async def test_activity_sweep_skips_unmatched(tmp_path):
    """进行中活动无法匹配 BAAS 模块时不扫活动（避免扫已结束的旧活动模块）"""
    now = int(time.time())
    running = GameEvent(
        id=32, title="纯中文活动标题", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["CodeBox", "SayBing"]
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    assert "activity_sweep" not in bridge.solves
    assert bridge.current_activity is None


@pytest.mark.asyncio
async def test_activity_sweep_alias_match(tmp_path):
    """纯中文标题活动通过人工映射表命中（如「笑笑闹闹」→ CN 服 LivelyandBusily）"""
    now = int(time.time())
    running = GameEvent(
        id=33, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["CodeBox", "LivelyandBusily"]
    bridge.banner_text = "距离结束还剩5天 笑笑闹闹 走走绕绕"
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    assert bridge.current_activity == "LivelyandBusily"
    assert bridge.solves.count("activity_sweep") == 1
    assert ACTIVITY_MODULE_ALIASES["笑笑闹闹"] == "LivelyandBusily"


@pytest.mark.asyncio
async def test_activity_sweep_alias_whitelist_gate(tmp_path):
    """别名命中的模块不在当前服白名单时不可用（如 JP 模块名在 CN 服）"""
    now = int(time.time())
    running = GameEvent(
        id=37, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["CodeBox"]  # 白名单里没有 LivelyandBusily
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    assert "activity_sweep" not in bridge.solves
    assert bridge.current_activity is None


@pytest.mark.asyncio
async def test_sweep_waits_for_banner_rotation(tmp_path):
    """轮播图当前页不是目标活动时，等待自动换页直到 OCR 识别到目标"""
    now = int(time.time())
    running = GameEvent(
        id=39, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    bridge.banner_module = False  # 模板不命中，走 OCR 轮询等待换页
    bridge.banner_text = [
        "海文迪 铁道失控事件",  # 第 1 次 OCR：火车活动页
        "海文迪 铁道失控事件",  # 第 2 次 OCR：还是火车活动页
        "笑笑闹闹 走走绕绕",   # 第 3 次 OCR：换页到目标
    ]
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    assert bridge.current_activity == "LivelyandBusily"
    assert bridge.solves.count("activity_sweep") == 1


@pytest.mark.asyncio
async def test_sweep_skips_when_banner_never_matches(tmp_path):
    """轮播图始终不是目标活动（如目标已下架）→ 等待超时后跳过活动扫荡"""
    now = int(time.time())
    running = GameEvent(
        id=41, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    bridge.banner_text = "海文迪 铁道失控事件"
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    ok = await engine._wait_for_activity_banner(["笑笑闹闹"], timeout=0.5)
    assert not ok
    # 超时后 run_sweep 内部会跳过 activity_sweep
    assert bridge.solves.count("activity_sweep") == 0


@pytest.mark.asyncio
async def test_banner_template_match_hits_first(tmp_path):
    """模板匹配优先：OCR 完全识别不出（艺术字）时，enter1 模板命中即确认当前页"""
    now = int(time.time())
    running = GameEvent(
        id=43, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    bridge.banner_text = ""  # OCR 吐不出任何字（艺术字场景）
    bridge.banner_module = "LivelyandBusily"  # 但模板匹配命中
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    assert bridge.current_activity == "LivelyandBusily"
    assert bridge.solves.count("activity_sweep") == 1


@pytest.mark.asyncio
async def test_banner_template_miss_falls_back_to_ocr(tmp_path):
    """模板不命中（如候选活动无 enter1 模板）时回退 OCR 模糊匹配"""
    now = int(time.time())
    running = GameEvent(
        id=45, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    bridge.banner_module = False  # 模板不命中，验证 OCR 兜底
    bridge.banner_text = "笑笑闹闹 走走绕绕"  # OCR 兜底命中
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
    )
    await engine.run_once()
    assert bridge.solves.count("activity_sweep") == 1


@pytest.mark.asyncio
async def test_banner_fuzzy_match_tolerates_ocr_errors(tmp_path):
    """OCR 错字时仍能模糊匹配（如「笑笑闹闹」被识别成「笑笑同闹」）"""
    assert Engine._fuzzy_contains("距离结束还剩5天 笑笑闹闹 走走绕绕", "笑笑闹闹")
    assert Engine._fuzzy_contains("笑笑同闹 走走绕绕", "笑笑闹闹")  # 闹→同 错字
    assert not Engine._fuzzy_contains("海文迪 铁道失控事件", "笑笑闹闹")
    assert Engine._banner_keywords("复刻活动【笑笑闹闹 走走绕绕】") == [
        "笑笑闹闹", "走走绕绕",
    ]

    # 艺术字场景：单字命中率匹配（OCR 错字/漏字多，多数单字出现即命中）
    assert Engine._fuzzy_contains("笑笑闹闹", "笑笑闹闹")  # 整词
    assert Engine._fuzzy_contains("笑闹闹", "笑笑闹闹")  # 漏 1 字：3/4 单字命中
    assert Engine._fuzzy_contains("笑笑司闹", "笑笑闹闹")  # 闹→司 错字：3/4 单字命中
    assert not Engine._fuzzy_contains("海文迪铁道失控", "笑笑闹闹")  # 单字不重叠
    # 非中文乱码（l/1/o/0）不应误命中纯中文关键词
    assert not Engine._fuzzy_contains("111000", "笑笑闹闹")


@pytest.mark.asyncio
async def test_collect_reward_after_all_tasks(tmp_path):
    """领取日程（collect_reward）在所有任务（含 arena）完成后执行"""
    bridge = FakeBridge(ap=100)
    engine = make_engine(
        bridge,
        events=[],
        data_dir=str(tmp_path),
        baas={"tasks": ["cafe_reward", "collect_reward", "lesson"]},
        sweep={"normal_tasks": []},
    )
    await engine.run_once()
    idx = {t: i for i, t in enumerate(bridge.solves)}
    # collect_reward 在 cafe_reward / lesson 之后，且在扫荡之前（任务阶段最后一个）
    assert idx["collect_reward"] > idx["cafe_reward"]
    assert idx["collect_reward"] > idx["lesson"]
    assert idx["collect_reward"] < idx["activity_sweep"]


async def _instant_sleep(delay):
    """测试替身：不真等冷却"""
    return None


@pytest.mark.asyncio
async def test_arena_loops_until_tickets_done(tmp_path, monkeypatch):
    """arena 自动重复：next_time>0 时异步等待冷却后继续，直到票用完（next_time=0）"""
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)  # 测试中不真等冷却
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
async def test_arena_interleaves_with_regular_tasks(tmp_path, monkeypatch):
    """arena 冷却等待期间穿插执行常规任务（asyncio.sleep 让出事件循环）"""
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    next_times = iter([55, 0])  # 第 1 场后冷却 55s，第 2 场结束

    class InterleaveBridge(FakeBridge):
        @property
        def last_next_time(self):
            try:
                return next(next_times)
            except StopIteration:
                return 0

    bridge = InterleaveBridge(ap=100)
    engine = make_engine(
        bridge,
        events=[],
        data_dir=str(tmp_path),
        baas={"tasks": ["cafe_reward", "arena", "lesson"]},
        sweep={"normal_tasks": []},
    )
    result = await engine.run_once()
    # arena 与常规任务都执行了
    assert bridge.solves.count("arena") == 2
    for t in ["cafe_reward", "lesson"]:
        assert t in bridge.solves
        assert t in result.executed_tasks
    # 锁串行化：任一时刻最多一个任务正在执行（solve 记录顺序无并发交叉）
    assert bridge.solves.count("arena") + bridge.solves.count("cafe_reward") + bridge.solves.count("lesson") == 4


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
