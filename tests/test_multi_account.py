"""多账号编排测试（MultiAccountRunner：串行、失败隔离、共享 Main 注入）"""
import pytest

from baas_plus.config import AppConfig
from baas_plus.multi_account import MultiAccountRunner
from baas_plus.store import Store


class FakeBridge:
    """最小 FakeBridge：满足 Engine.run_once 全流程（无任务/无活动/无扫荡路径）"""

    def __init__(self, ap=120):
        self.ap = ap
        self.started = False
        self.solves: list[str] = []
        self._main = None  # Runner 注入共享 Main 的位置

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
        return 0

    def get_ap(self):
        return self.ap

    def restart_simulator(self):
        return True

    def check_baas_update(self):
        return None

    def get_baas_sweep_config(self):
        return {"mainlinePriority": [], "hardPriority": []}

    def get_current_activity(self):
        return None

    def set_current_activity(self, module_name):
        pass

    def set_sweep_tasks(self, normal_tasks, hard_tasks):
        pass

    def set_activity_sweep(self, task_number, times):
        pass

    def list_activity_modules(self):
        return []

    def activity_module_available(self, module_name):
        return False

    def ocr_banner(self):
        return ""

    def match_banner_activity(self, candidates):
        return None

    def enter_current_activity(self, timeout=8.0):
        return False

    def solve_activity_sweep_after_enter(self):
        return True

    def solve_activity_explore_mission(self):
        return True

    def go_main_page(self):
        return True

    def close_announcement_popups(self, timeout=30.0):
        return True

    def stop(self):
        pass


class FakeFetcher:
    async def fetch_all(self):
        return []


def make_config(tmp_path, accounts):
    return AppConfig(data_dir=str(tmp_path), accounts=accounts)


def make_runner(config, tmp_path, bridge_factory=None):
    store = Store(tmp_path / "baas_plus.db")
    return MultiAccountRunner(
        config,
        store=store,
        bridge_factory=bridge_factory or (lambda acc: FakeBridge()),
        fetcher_factory=lambda server: FakeFetcher(),
    )


@pytest.mark.asyncio
async def test_run_all_serial_order(tmp_path):
    """run_all 按账号顺序串行执行（每个账号独立 bridge/模拟器实例）"""
    config = make_config(
        tmp_path,
        [
            {"name": "主号", "simulator": {"instance": 0}, "baas": {"tasks": []}, "sweep": {"activity_first": False}},
            {"name": "小号", "simulator": {"instance": 1}, "baas": {"tasks": []}, "sweep": {"activity_first": False}},
        ],
    )
    bridges: list[FakeBridge] = []

    def factory(acc):
        b = FakeBridge()
        bridges.append(b)
        return b

    runner = make_runner(config, tmp_path, bridge_factory=factory)
    results = await runner.run_all()
    # 返回顺序 = 配置顺序
    assert [aid for aid, _ in results] == [a.id for a in config.accounts]
    # 每个账号独立 bridge 且都执行了
    assert len(bridges) == 2
    assert all(b.started for b in bridges)
    assert all(r.status == "success" for _, r in results)
    # 记录带账号维度
    records = runner.store.list_records()
    assert {r["account"] for r in records} == {a.id for a in config.accounts}


@pytest.mark.asyncio
async def test_disabled_account_skipped(tmp_path):
    """enabled=False 的账号不参与 run_all"""
    config = make_config(
        tmp_path,
        [
            {"name": "A", "enabled": True, "baas": {"tasks": []}},
            {"name": "B", "enabled": False, "baas": {"tasks": []}},
            {"name": "C", "enabled": True, "baas": {"tasks": []}},
        ],
    )
    runner = make_runner(config, tmp_path)
    results = await runner.run_all()
    ids = [aid for aid, _ in results]
    assert config.accounts[1].id not in ids
    assert len(results) == 2


@pytest.mark.asyncio
async def test_failure_isolation(tmp_path):
    """账号 A 失败不影响账号 B 继续执行（失败隔离）"""

    class BoomBridge(FakeBridge):
        def start_simulator(self):
            raise RuntimeError("模拟器启动失败")

    config = make_config(
        tmp_path,
        [
            {"name": "A", "baas": {"tasks": []}},
            {"name": "B", "baas": {"tasks": []}},
        ],
    )
    called: list[str] = []

    def factory(acc):
        called.append(acc.name)
        return BoomBridge() if acc.name == "A" else FakeBridge()

    runner = make_runner(config, tmp_path, bridge_factory=factory)
    results = await runner.run_all()
    by_name = {runner.get_account(aid).name: (aid, r) for aid, r in results}
    assert by_name["A"][1].status == "failed"
    assert by_name["B"][1].status == "success"
    assert called == ["A", "B"]  # B 在 A 失败后仍被执行


@pytest.mark.asyncio
async def test_run_account_by_id_and_name(tmp_path):
    """run_account 支持按 id 或 name 指定"""
    config = make_config(
        tmp_path,
        [
            {"name": "主号", "baas": {"tasks": []}},
            {"name": "小号", "baas": {"tasks": []}},
        ],
    )
    runner = make_runner(config, tmp_path)
    aid, result = await runner.run_account("小号")  # 按 name
    assert aid == config.accounts[1].id
    assert result.status == "success"
    aid2, _ = await runner.run_account(config.accounts[0].id)  # 按 id
    assert aid2 == config.accounts[0].id


def test_get_account_not_found(tmp_path):
    config = make_config(tmp_path, [{"name": "主号"}])
    runner = make_runner(config, tmp_path)
    with pytest.raises(ValueError):
        runner.get_account("不存在的账号")


@pytest.mark.asyncio
async def test_shared_main_injected_and_reclaimed(tmp_path):
    """共享 BAAS Main：Runner 注入到 bridge，执行后取回（OCR 服务器进程只起一个）"""
    config = make_config(tmp_path, [{"name": "A", "baas": {"tasks": []}}])
    # 用默认 bridge_factory（真实 BaasBridge 构造不触发 BAAS import，仅存引用）
    runner = MultiAccountRunner(
        config, store=Store(tmp_path / "baas_plus.db"), fetcher_factory=lambda server: FakeFetcher()
    )

    class FakeMain:
        pass

    fake_main = FakeMain()
    runner._main = fake_main
    engine = runner._new_engine(config.accounts[0])
    assert engine.bridge._main is fake_main
    # run_account 结束后 Runner 取回（模拟第一个账号创建后回填）
    runner._main = getattr(engine.bridge, "_main", None)
    assert runner._main is fake_main


@pytest.mark.asyncio
async def test_same_server_fetcher_shared(tmp_path):
    """同服账号共享 ActivityFetcher 实例（数据源只建一个）"""
    config = make_config(
        tmp_path,
        [
            {"name": "A", "baas": {"tasks": []}, "activity": {"server": "cn"}},
            {"name": "B", "baas": {"tasks": []}, "activity": {"server": "cn"}},
            {"name": "C", "baas": {"tasks": []}, "activity": {"server": "jp"}},
        ],
    )
    runner = make_runner(config, tmp_path)
    e1 = runner._new_engine(config.accounts[0])
    e2 = runner._new_engine(config.accounts[1])
    e3 = runner._new_engine(config.accounts[2])
    assert e1.fetcher is e2.fetcher  # 同服复用
    assert e1.fetcher is not e3.fetcher  # 不同服独立
