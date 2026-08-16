"""核心引擎测试（mock bridge / fetcher，不依赖真实 BAAS 与网络）"""
import time

import json
import pytest

from baas_plus.activity import (
    ACTIVITY_MODULE_ALIASES,
    EventType,
    GameEvent,
)
from baas_plus.baas_bridge import compute_sweep_times
from baas_plus.config import AppConfig
from baas_plus.engine import ACTIVITY_ENTER_MAX_RETRIES, Engine, HARD_MAX_TIMES


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
        self.baas_sweep_config = {"mainlinePriority": [], "hardPriority": []}
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

    def restart_simulator(self):
        self.restarts = getattr(self, "restarts", 0) + 1
        return True

    def get_baas_sweep_config(self):
        return self.baas_sweep_config

    def get_current_activity(self):
        return getattr(self, "current_activity_record", None)

    def check_baas_update(self):
        return None

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

    def enter_current_activity(self, timeout=8.0):
        self.solves.append("enter_current_activity")
        return getattr(self, "enter_ok", True)

    def activity_ended_popup(self):
        return getattr(self, "ended_popup", False)

    def solve_activity_sweep_after_enter(self):
        self.solves.append("activity_sweep")
        return True

    def solve_activity_explore_mission(self):
        self.solves.append("explore_activity_mission")
        return True

    def go_main_page(self):
        self.solves.append("go_main_page")
        return True

    def close_announcement_popups(self, timeout=30.0):
        return True

    def stop(self):
        pass


class FakeFetcher:
    def __init__(self, events=None):
        self.events = events or []

    async def fetch_all(self):
        return self.events


def make_engine(bridge=None, events=None, data_dir=None, account_id="acc_test", **cfg_kwargs):
    if data_dir is None:
        import tempfile

        data_dir = tempfile.mkdtemp(prefix="baas_plus_test_")
    # cfg_kwargs 的键与 AccountConfig 字段一致（baas/sweep/simulator/activity），
    # 作为首个账号配置传入（多账号结构）；account_id 固定，保证同 data_dir 下
    # 多次运行属于同一账号（活动状态/执行记录正确共享）
    app_kwargs: dict = {"data_dir": data_dir}
    app_kwargs["accounts"] = [{"id": account_id, **cfg_kwargs}]
    config = AppConfig(**app_kwargs)
    # 默认给一个活动模块便于推图测试；显式传 current_activity 可覆盖（如 ""）
    config.accounts[0].baas.current_activity = (cfg_kwargs.get("baas") or {}).get(
        "current_activity", "SayBing"
    )
    from pathlib import Path

    from baas_plus.store import Store

    account = config.accounts[0]
    store = Store(Path(data_dir) / "baas_plus.db")
    engine = Engine(
        account,
        account_id=account.id,
        store=store,
        bridge=bridge or FakeBridge(),
        fetcher=FakeFetcher(events),
        notify=config.notify,
    )
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
    from pathlib import Path

    from baas_plus.store import Store

    store = Store(Path(engine.store.db_path).parent / "x.db")
    store.mark_activity_seen(
        engine.account_id,
        GameEvent(id=9, title="A", start_at=0, end_at=int(time.time()) + 1000, event_type=EventType.EVENT),
    )
    engine.store = store
    assert engine.has_active_activity()


def test_has_active_activity_excludes_assault(tmp_path):
    """总力战/大决战等 assault 事件不应触发「有活动优先扫活动」"""
    from pathlib import Path

    from baas_plus.store import Store

    engine = make_engine(FakeBridge(), events=[], data_dir=str(tmp_path))
    store = Store(Path(engine.store.db_path).parent / "x.db")
    store.mark_activity_seen(
        engine.account_id,
        GameEvent(id=10, title="总力战", start_at=0, end_at=int(time.time()) + 1000, event_type=EventType.ASSAULT),
    )
    store.mark_activity_seen(
        engine.account_id,
        GameEvent(id=11, title="卡池", start_at=0, end_at=int(time.time()) + 1000, event_type=EventType.CARD),
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
    assert not engine.store.is_activity_seen(engine.account_id, not_started)

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
    ] * 2  # 推图阶段消耗一轮，扫荡阶段再消耗一轮
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
async def test_activity_sweep_ensures_and_restores_main_page(tmp_path):
    """活动扫荡的轮播图导航：扫描前先确保 BA 在主界面，扫荡结束后再回到主界面

    回归：轮播图区域（banner_region）只在主界面存在，若扫描轮播图时 BA 不在
    主界面（如上一任务遗留的活动菜单/任务列表页面），模板/OCR 会读到错误内容，
    enter1 固定坐标点击也可能落在无关按钮上；且活动扫荡屏蔽了 to_main_page
    （solve_activity_sweep_after_enter），扫荡结束后需显式回主界面。
    """
    now = int(time.time())
    running = GameEvent(
        id=51, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )

    class BannerOrderBridge(FakeBridge):
        """记录 go_main_page / 轮播图扫描的先后顺序（solves 只记操作不记扫描）"""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.order: list[str] = []

        def go_main_page(self):
            self.order.append("go_main_page")
            return super().go_main_page()

        def match_banner_activity(self, candidates):
            self.order.append("match_banner_activity")
            return super().match_banner_activity(candidates)

        def ocr_banner(self):
            self.order.append("ocr_banner")
            return super().ocr_banner()

    bridge = BannerOrderBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    bridge.banner_module = False  # 模板不命中 → 走 OCR 轮询，验证扫描发生在回主界面之后
    bridge.banner_text = "笑笑闹闹 走走绕绕"
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
        activity={"push_before_sweep": False},  # 只走活动扫荡一条路径，顺序确定
    )
    await engine.run_once()
    # 扫描轮播图前先回主界面：首次 go_main_page 先于首次模板匹配 / OCR 扫描
    assert bridge.order[0] == "go_main_page"
    assert bridge.order.index("go_main_page") < bridge.order.index("match_banner_activity")
    assert bridge.order.index("go_main_page") < bridge.order.index("ocr_banner")
    # 扫荡结束后回到主界面：末次 go_main_page 在 activity_sweep 之后
    first, last = {}, {}
    for i, s in enumerate(bridge.solves):
        first.setdefault(s, i)
        last[s] = i
    assert first["go_main_page"] < first["enter_current_activity"]
    assert last["go_main_page"] > last["activity_sweep"]


@pytest.mark.asyncio
async def test_activity_sweep_retries_on_ended_popup(tmp_path):
    """点开活动后弹出「活动时间已结束」→ 返回主界面重新尝试打开活动，最终成功

    轮播图换页瞬间点击可能落在已结束的活动上：前两次进入被结束弹窗打断，
    第三次成功进入活动菜单并执行扫荡。每次重试都先回主界面再等轮播图。
    """
    now = int(time.time())
    running = GameEvent(
        id=61, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )

    class RetryBridge(FakeBridge):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.enter_attempts = 0
            self.popup_checks = 0

        def enter_current_activity(self, timeout=8.0):
            self.solves.append("enter_current_activity")
            self.enter_attempts += 1
            return self.enter_attempts >= 3  # 前两次被结束弹窗打断，第三次成功

        def activity_ended_popup(self):
            self.popup_checks += 1
            return True  # 进入失败原因始终是「活动时间已结束」弹窗

    bridge = RetryBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
        activity={"push_before_sweep": False},
    )
    result = await engine.run_once()
    assert bridge.solves.count("enter_current_activity") == 3
    assert bridge.solves.count("activity_sweep") == 1
    # 每次重试前都回主界面（第 3 次成功后扫荡结束还会再回一次主界面）
    assert bridge.solves.count("go_main_page") >= 3
    assert bridge.popup_checks == 2
    assert result.swept == ["activity:1(auto,LivelyandBusily)"]


@pytest.mark.asyncio
async def test_activity_sweep_skipped_when_ended_popup_persists(tmp_path):
    """连续多次进入活动均提示「活动时间已结束」→ 跳过活动扫荡，不回退 BAAS 原生扫荡

    活动确实已结束时，回退 BAAS 原生 activity_sweep 只会在结束弹窗上反复
    点击卡死，因此直接跳过该活动。
    """
    now = int(time.time())
    running = GameEvent(
        id=62, title="复刻活动【笑笑闹闹 走走绕绕】", start_at=now - 3600,
        end_at=now + 86400, event_type=EventType.EVENT,
    )

    class EndedBridge(FakeBridge):
        def enter_current_activity(self, timeout=8.0):
            self.solves.append("enter_current_activity")
            return False

        def activity_ended_popup(self):
            return True

    bridge = EndedBridge(ap=500)
    bridge.activity_modules = ["LivelyandBusily"]
    engine = make_engine(
        bridge,
        events=[running],
        data_dir=str(tmp_path),
        baas={"tasks": [], "current_activity": ""},
        activity={"push_before_sweep": False},
    )
    result = await engine.run_once()
    assert bridge.solves.count("activity_sweep") == 0
    assert bridge.solves.count("enter_current_activity") == ACTIVITY_ENTER_MAX_RETRIES
    assert bridge.solves.count("go_main_page") == ACTIVITY_ENTER_MAX_RETRIES
    assert result.swept == []


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
    # collect_reward 在 cafe_reward / lesson 之后，且在扫荡之后（所有任务最后）
    assert idx["collect_reward"] > idx["cafe_reward"]
    assert idx["collect_reward"] > idx["lesson"]
    assert idx["collect_reward"] > idx["activity_sweep"]


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
async def test_sweep_restarts_simulator_when_ap_read_fails(tmp_path):
    """体力读取失败（游戏/模拟器失联）→ 自动重启一次模拟器后重试成功"""
    class ApFailOnceBridge(FakeBridge):
        def __init__(self):
            super().__init__(ap=120)
            self.ap_calls = 0

        def get_ap(self):
            self.ap_calls += 1
            return -1 if self.ap_calls == 1 else 120

    bridge = ApFailOnceBridge()
    engine = make_engine(
        bridge, events=[], data_dir=str(tmp_path),
        sweep={"normal_tasks": ["15-1-99"], "activity_first": False},
    )
    swept = await engine.run_sweep()
    assert bridge.restarts == 1  # 重启恰好一次
    assert bridge.ap_calls == 2  # 重启后再读一次
    assert swept == ["15-1-12"]  # 120 体力按 auto 重算
    assert engine.result.ap_before_sweep == 120


@pytest.mark.asyncio
async def test_sweep_skips_when_simulator_restart_fails(tmp_path):
    """重启模拟器失败 → 跳过扫荡（不抛异常、不崩溃）"""
    class ApFailBridge(FakeBridge):
        def get_ap(self):
            return -1

        def restart_simulator(self):
            self.restarts = getattr(self, "restarts", 0) + 1
            return False

    bridge = ApFailBridge()
    engine = make_engine(
        bridge, events=[], data_dir=str(tmp_path),
        sweep={"normal_tasks": ["15-1-99"]},
    )
    swept = await engine.run_sweep()
    assert swept == []
    assert engine.result.ap_before_sweep == -1
    assert bridge.restarts == 1
    assert "normal_task" not in bridge.solves


@pytest.mark.asyncio
async def test_sweep_no_restart_when_disabled(tmp_path):
    """auto_restart_on_failure=False 时不重启模拟器，直接跳过扫荡"""
    class ApFailBridge(FakeBridge):
        def get_ap(self):
            return -1

    bridge = ApFailBridge()
    engine = make_engine(
        bridge, events=[], data_dir=str(tmp_path),
        sweep={"normal_tasks": ["15-1-99"]},
        simulator={"auto_restart_on_failure": False},
    )
    swept = await engine.run_sweep()
    assert swept == []
    assert engine.result.ap_before_sweep == -1
    assert not getattr(bridge, "restarts", 0)


@pytest.mark.asyncio
async def test_sweep_fallback_to_baas_config(tmp_path):
    """BAAS-Plus 扫荡列表为空时，回退读取 BAAS 配置里的 mainlinePriority/hardPriority"""
    bridge = FakeBridge(ap=120)
    bridge.baas_sweep_config = {
        "mainlinePriority": ["5-1-3", "6-1-2"],
        "hardPriority": ["20-1-1"],
    }
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


def test_parse_sweep_list_pollution():
    """历史嵌套转义污染能被 _parse_sweep_list 清理为干净项"""
    from baas_plus.baas_bridge import _parse_sweep_list

    # 正常 str
    assert _parse_sweep_list("3-3-3,9-3-3") == ["3-3-3", "9-3-3"]
    # list（旧版误写入 JSON 数组）
    assert _parse_sweep_list(["3-3-3", "9-3-3"]) == ["3-3-3", "9-3-3"]
    # 一层污染：str(list) 后 split 的垃圾元素
    assert _parse_sweep_list("['3-3-3', '9-3-3']") == ["3-3-3", "9-3-3"]
    # 深层嵌套转义（主人实机截图形态）
    deep = "['[\\\\'[\\\\\\\\\\\\'[\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\'3-3-3"
    assert _parse_sweep_list(deep) == ["3-3-3"]
    # 空/垃圾
    assert _parse_sweep_list("") == []
    assert _parse_sweep_list("garbage") == []


def test_baas_sweep_config_file_only(tmp_path):
    """纯文件读写 BAAS config.json：无需 Baas_thread，WebUI 保存配置场景可用"""
    from baas_plus.baas_bridge import BaasBridge
    from baas_plus.config import AppConfig

    repo = tmp_path / "baas"
    cfg_dir = repo / "config" / "cn"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.json"
    # 模拟主人实机的历史污染（嵌套转义垃圾数组）
    cfg_file.write_text(
        '{\n  "mainlinePriority": "5-1-3,6-1-2",\n'
        '  "hardPriority": ["\'[\\\\\'[\\\\\\\\\\\\\'3-3-3", "\'9-3-3"]\n}',
        encoding="utf-8",
    )
    bridge = BaasBridge(AppConfig(accounts=[{"baas": {"repo_dir": str(repo), "config_dir": "cn"}}]))

    # 读取：自动清理污染，且不需要 Baas_thread
    cfg = bridge.get_baas_sweep_config()
    assert cfg == {"mainlinePriority": ["5-1-3", "6-1-2"], "hardPriority": ["3-3-3", "9-3-3"]}

    # 写入：join 成字符串，恢复 BAAS 原生 str 类型
    bridge.set_sweep_tasks(["5-1-5"], ["20-1-2"])
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["mainlinePriority"] == "5-1-5"
    assert data["hardPriority"] == "20-1-2"
    assert isinstance(data["hardPriority"], str)


def test_sync_sweep_push_back_to_baas(tmp_path):
    """保存配置时：BAAS-Plus 有扫荡配置则写回 BAAS config.json（用户确认后修改 BAAS）"""
    from baas_plus.baas_bridge import BaasBridge
    from baas_plus.config import AppConfig

    repo = tmp_path / "baas2"
    cfg_dir = repo / "config" / "cn"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text('{\n  "mainlinePriority": "",\n  "hardPriority": ""\n}', encoding="utf-8")
    config = AppConfig(
        accounts=[
            {
                "baas": {"repo_dir": str(repo), "config_dir": "cn"},
                "sweep": {"normal_tasks": ["5-1-3"], "hard_tasks": ["20-1-1"]},
            }
        ]
    )
    bridge = BaasBridge(config)
    result = bridge.sync_sweep_from_baas()
    assert result["pushed"] is True
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["mainlinePriority"] == "5-1-3"
    assert data["hardPriority"] == "20-1-1"


@pytest.mark.asyncio
async def test_push_uses_baas_recorded_activity(tmp_path):
    """无手动配置但 BAAS 记录 current_game_activity 正确 → 推图/扫荡照常执行"""
    event = GameEvent(
        id=99, title="中文标题新活动", start_at=int(time.time()) - 10,
        end_at=int(time.time()) + 86400, event_type=EventType.EVENT,
    )
    bridge = FakeBridge(ap=200)
    bridge.current_activity_record = "SayBing"
    engine = make_engine(
        bridge, events=[event], data_dir=str(tmp_path),
        baas={"current_activity": ""},
    )
    result = await engine.run_once()
    assert bridge.current_activity == "SayBing"
    assert "explore_activity_story" in bridge.solves
    assert "activity_sweep" in bridge.solves
    assert result.status == "success"


def test_check_baas_update(tmp_path, monkeypatch):
    """BAAS 更新检查：stable 过滤 + 版本比较 + 主版本线兼容判断"""
    import io
    from unittest.mock import patch

    from baas_plus.baas_bridge import BaasBridge
    from baas_plus.config import AppConfig

    repo = tmp_path / "baas3"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.4.2"\n', encoding="utf-8")
    bridge = BaasBridge(AppConfig(accounts=[{"baas": {"repo_dir": str(repo)}}]))

    fake_releases = [
        {"tag_name": "v1.5.0-beta", "prerelease": True, "draft": False},
        {"tag_name": "v1.4.3", "prerelease": False, "draft": False,
         "html_url": "https://github.com/x/releases/v1.4.3", "body": "fixes"},
        {"tag_name": "v1.4.2", "prerelease": False, "draft": False},
    ]
    fake_resp = io.BytesIO(json.dumps(fake_releases).encode())

    with patch("urllib.request.urlopen", return_value=fake_resp):
        update = bridge.check_baas_update()
    assert update is not None
    assert update["local"] == "1.4.2"
    assert update["latest"] == "1.4.3"
    assert update["compatible"] is True  # 同 1.x 主版本线
    assert "v1.4.3" in update["url"]

    # 已是新版本 → 无更新
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.4.3"\n', encoding="utf-8")
    fake_resp = io.BytesIO(json.dumps(fake_releases).encode())
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert bridge.check_baas_update() is None

    # 主版本线不同（未来 2.0 转正）→ compatible=False 仍提示
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.4.3"\n', encoding="utf-8")
    fake2 = [{"tag_name": "v2.0.0", "prerelease": False, "draft": False,
              "html_url": "https://github.com/x/releases/v2.0.0", "body": ""}]
    fake_resp = io.BytesIO(json.dumps(fake2).encode())
    with patch("urllib.request.urlopen", return_value=fake_resp):
        update = bridge.check_baas_update()
    assert update["latest"] == "2.0.0"
    assert update["compatible"] is False
