"""BaasBridge 初始化顺序测试（mock BAAS，不依赖真实环境）

回归：create_baas 必须先 set_ocr 再 init_all_data，否则
ocr_img_pass_method 保持 None → OCR 调用报 Invalid pass_method None。
"""
import pytest

from baas_plus.baas_bridge import BaasBridge
from baas_plus.config import AppConfig


class FakeU2:
    """模拟 uiautomator2 连接：记录 app_start 调用"""

    def __init__(self):
        self.last_start = None

    def app_start(self, package_name, activity=None):
        self.last_start = (package_name, activity)


class FakeBaasThread:
    """记录调用顺序的假 Baas_thread"""

    def __init__(self, config_set, *args, **kwargs):
        self.config_set = config_set
        self.calls: list[str] = []
        self.ocr = None
        self.ocr_img_pass_method = None
        self.package_name = "com.RoamingStar.BlueArchive"
        self.server = "CN"
        self.activity_name = None
        self.u2 = FakeU2()
        # 真实 Baas_thread.__init__ 会初始化 next_time=0（BAAS 调度器用）
        self.next_time = 0

    def set_ocr(self, ocr):
        self.calls.append("set_ocr")
        self.ocr = ocr
        self.ocr_img_pass_method = 0

    def init_all_data(self):
        self.calls.append("init_all_data")

    def set_adb_address(self, addr):
        self.calls.append("set_adb_address")

    def to_main_page(self):
        self.calls.append("to_main_page")

    def solve(self, task):
        return True


class FakeMain:
    def __init__(self, ocr_needed=None):
        self.ocr = object()


def fake_import_baas(repo_dir):
    class FakeConfigSet:
        static_config = type("SC", (), {"package_name": {"官服": "com.RoamingStar.BlueArchive"}})()

        def __init__(self, config_dir=None):
            self.config_dir = config_dir
            self._data = {}

        def get(self, key, default=None):
            if key == "server":
                return "官服"
            return self._data.get(key, default)

        def set(self, key, value):
            self._data[key] = value

    return FakeBaasThread, FakeConfigSet, FakeMain


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr("baas_plus.baas_bridge.import_baas", fake_import_baas)
    monkeypatch.setattr("baas_plus.baas_bridge.repair_user_config", lambda *a, **k: None)
    config = AppConfig()
    config.baas.repo_dir = "/fake/baas"
    return BaasBridge(config)


def test_create_baas_sets_ocr_before_init(bridge):
    """set_ocr 必须在 init_all_data 之前调用（Invalid pass_method 回归）"""
    baas = bridge.create_baas("127.0.0.1:16384")
    assert baas.calls.index("set_ocr") < baas.calls.index("init_all_data")
    assert baas.ocr is not None


def test_create_baas_uses_adb_address_from_arg(bridge):
    """发现到 adb 地址时，必须在 init_all_data（设备连接）之前写入 BAAS config

    回归：旧实现把 set_adb_address 放在 init_all_data 之后（且 BAAS master 根本没
    这个方法，是空操作），导致 BAAS 仍按 config 里的旧端口连接。
    现在应在 init 前把 adbIP/adbPort 写进 ConfigSet，让 Connection 建连时用对端口。
    """
    baas = bridge.create_baas("127.0.0.1:16600")
    assert baas.config_set.get("adbIP") == "127.0.0.1"
    assert baas.config_set.get("adbPort") == "16600"
    # 覆写发生在 init_all_data（连接建立）之前
    assert baas.calls.index("set_ocr") < baas.calls.index("init_all_data")


def test_create_baas_no_adb_keeps_config(bridge):
    """不带 adb 地址时不覆写 config（保持原有 adbIP/adbPort）"""
    bridge.create_baas()
    assert bridge.baas_thread.config_set.get("adbIP") is None
    assert bridge.baas_thread.config_set.get("adbPort") is None


def test_parse_adb_address_with_port():
    ip, port = BaasBridge._parse_adb_address("127.0.0.1:16384")
    assert (ip, port) == ("127.0.0.1", "16384")


def test_parse_adb_address_without_port():
    ip, port = BaasBridge._parse_adb_address("emulator-5554")
    assert (ip, port) == ("emulator-5554", "")


def test_create_baas_apply_game_package(bridge):
    baas = bridge.create_baas()
    config_set = baas.config_set
    assert config_set.static_config.package_name["官服"] == "com.RoamingStar.BlueArchive"
    # Baas_thread.package_name 应同步为配置包名（restart/launch_game 实际使用值）
    assert baas.package_name == "com.RoamingStar.BlueArchive"


def test_launch_game_uses_configured_package(bridge):
    bridge.create_baas()
    bridge.config.baas.game_package_name = "com.custom.bluearchive"
    bridge.launch_game()
    u2 = bridge.baas_thread.u2
    # BAAS-Plus 用配置的包名显式启动 BA（CN 服 activity=None）
    assert u2.last_start == ("com.custom.bluearchive", None)
    # 启动后进入主界面
    assert "to_main_page" in bridge.baas_thread.calls
    # 同步后的 Baas_thread.package_name
    assert bridge.baas_thread.package_name == "com.custom.bluearchive"


class FakeBaasThreadWithImg:
    """带截图帧的假 Baas_thread（供模板匹配测试）"""

    def __init__(self, img):
        self.latest_img_array = img
        self.ratio = 1.0
        self.screenshotted = 0

    def update_screenshot_array(self):
        self.screenshotted += 1


def _make_screen_with_button():
    """构造 1280x720 假主页：随机背景 + 轮播图区域 (1109,133,1280,281) 内一个特征按钮"""
    import cv2
    import numpy as np

    rng = np.random.default_rng(42)
    screen = rng.integers(0, 60, (720, 1280, 3), dtype=np.uint8)
    # 按钮：黄色圆角方块（放在轮播图区域内，对应 BAAS enter1 位置附近）
    cv2.rectangle(screen, (1181, 180), (1201, 200), (240, 220, 80), -1)
    cv2.circle(screen, (1191, 190), 8, (60, 60, 240), -1)
    return screen


def test_match_banner_activity_hits_target(bridge, tmp_path, monkeypatch):
    """enter1 模板命中轮播图区域 → 返回目标模块（绕开 OCR 艺术字问题）"""
    import cv2

    screen = _make_screen_with_button()
    bridge.baas_thread = FakeBaasThreadWithImg(screen)
    # 模板 = 屏幕上按钮的实际像素（模拟 BAAS 活动模块自带模板）
    tpl = screen[180:200, 1181:1201].copy()
    tpl_path = tmp_path / "enter1.png"
    cv2.imwrite(str(tpl_path), tpl)
    monkeypatch.setattr(
        bridge, "_activity_template_path", lambda mod, fname: str(tpl_path)
    )
    assert bridge.match_banner_activity(["LivelyandBusily"]) == "LivelyandBusily"
    assert bridge.baas_thread.screenshotted == 1  # 确认先刷新了截图帧


def test_match_banner_activity_rejects_wrong_module(bridge, tmp_path, monkeypatch):
    """非当前页活动的模板（随机噪声）不应命中，返回 None"""
    import cv2
    import numpy as np

    screen = _make_screen_with_button()
    bridge.baas_thread = FakeBaasThreadWithImg(screen)
    rng = np.random.default_rng(7)
    noise_tpl = rng.integers(0, 255, (20, 22, 3), dtype=np.uint8)
    tpl_path = tmp_path / "enter1.png"
    cv2.imwrite(str(tpl_path), noise_tpl)
    monkeypatch.setattr(
        bridge, "_activity_template_path", lambda mod, fname: str(tpl_path)
    )
    assert bridge.match_banner_activity(["HighlanderRailroadExplosionIncident"]) is None


def test_match_banner_activity_no_screenshot(bridge):
    """无截图帧时返回 None（不崩溃）"""
    bridge.baas_thread = FakeBaasThreadWithImg(None)
    assert bridge.match_banner_activity(["LivelyandBusily"]) is None


class FakeOcrForRegion:
    """返回固定文本的假 OCR（忽略图像内容，仅验证区域 OCR 管道）"""

    def __init__(self, text):
        self.text = text

    def ocr_for_single_line(self, **kwargs):
        return self.text


class FakeBaasThreadForRegionOcr:
    """带固定 OCR 结果与截图帧的假 Baas_thread（供区域 OCR / 结束弹窗测试）"""

    def __init__(self, text, img=None):
        self.ocr = FakeOcrForRegion(text)
        self.latest_img_array = img
        self.ratio = 1.0
        self.refreshes = 0

    def update_screenshot_array(self):
        self.refreshes += 1


def _blank_screen():
    import numpy as np

    return np.zeros((720, 1280, 3), dtype=np.uint8)


def test_activity_ended_popup_detected(bridge):
    """OCR 到「活动时间已结束」→ 返回 True（先刷新截图帧再识别）"""
    baas = FakeBaasThreadForRegionOcr("活动时间已结束", img=_blank_screen())
    bridge.baas_thread = baas
    assert bridge.activity_ended_popup() is True
    assert baas.refreshes == 1


def test_activity_ended_popup_not_detected(bridge):
    """区域 OCR 到其他文本（如正常活动页内容）→ 返回 False"""
    baas = FakeBaasThreadForRegionOcr("活动进行中", img=_blank_screen())
    bridge.baas_thread = baas
    assert bridge.activity_ended_popup() is False


def test_activity_ended_popup_no_frame(bridge):
    """无截图帧 → 返回 False 不崩溃"""
    baas = FakeBaasThreadForRegionOcr("活动时间已结束", img=None)
    bridge.baas_thread = baas
    assert bridge.activity_ended_popup() is False


def test_enter_current_activity_returns_early_on_ended_popup(bridge, monkeypatch, no_sleep):
    """进入活动菜单检测期间识别到「活动时间已结束」→ 提前返回 False（不白等超时）"""
    import sys

    class FakeImage:
        @staticmethod
        def compare_image(baas, feat):
            return False  # 永远检测不到 activity_menu

    class FakeCore:
        image = FakeImage()

    monkeypatch.setitem(sys.modules, "core", FakeCore())  # 让 `from core import image` 成功
    thread = FakeBaasThreadForRegionOcr("活动时间已结束", img=_blank_screen())
    thread.clicked = None

    def _click(x, y):
        thread.clicked = (x, y)

    thread.click = _click
    bridge.baas_thread = thread
    assert bridge.enter_current_activity() is False
    assert thread.clicked == (1196, 195)  # 先点了 enter1
    assert thread.refreshes >= 1


class FakeBaasThreadForAp:
    """带可配置截图帧序列的假 Baas_thread（供 get_ap 测试）

    frames 中的每个元素依次成为一次 update_screenshot_array() 后的
    latest_img_array（用尽后保持最后一次的值）。
    """

    def __init__(self, frames, ap_result=120):
        self._frames = list(frames)
        self.latest_img_array = None
        self.ratio = 1.0
        self.ap_result = ap_result
        self.refreshes = 0
        self.get_ap_calls = 0

    def update_screenshot_array(self):
        self.refreshes += 1
        if self._frames:
            self.latest_img_array = self._frames.pop(0)

    def get_ap(self, is_main_page=False):
        self.get_ap_calls += 1
        return self.ap_result


@pytest.fixture
def no_sleep(monkeypatch):
    """禁用重试等待，避免测试慢 2 秒"""
    monkeypatch.setattr("baas_plus.baas_bridge.time.sleep", lambda s: None)


def test_get_ap_refreshes_screenshot_before_ocr(bridge, no_sleep):
    """get_ap 必须先刷新截图帧再 OCR（latest_img_array 空帧回归）"""
    baas = FakeBaasThreadForAp(frames=[object()])
    bridge.baas_thread = baas
    assert bridge.get_ap() == 120
    assert baas.refreshes == 1
    assert baas.get_ap_calls == 1


def test_get_ap_returns_minus_one_when_no_frame(bridge, no_sleep):
    """截图帧一直为空 → 返回 -1（引擎跳过扫荡），而不是抛 TypeError"""
    baas = FakeBaasThreadForAp(frames=[None, None, None])
    bridge.baas_thread = baas
    assert bridge.get_ap() == -1
    assert baas.get_ap_calls == 0  # 无有效帧时绝不调用 BAAS OCR


def test_get_ap_recovers_when_screenshot_returns(bridge, no_sleep):
    """前两次截图失败、第三次恢复 → 返回体力值（重试生效）"""
    baas = FakeBaasThreadForAp(frames=[None, None, object()])
    bridge.baas_thread = baas
    assert bridge.get_ap() == 120
    assert baas.refreshes == 3
    assert baas.get_ap_calls == 1


def test_get_ap_returns_minus_one_when_ocr_raises(bridge, no_sleep):
    """BAAS get_ap 内部 OCR 异常（NoneType 下标等）→ 返回 -1，不向上抛"""
    baas = FakeBaasThreadForAp(frames=[object()] * 3)

    def failing_get_ap(is_main_page=False):
        raise TypeError("'NoneType' object is not subscriptable")

    baas.get_ap = failing_get_ap
    bridge.baas_thread = baas
    assert bridge.get_ap() == -1


def test_restart_simulator_sequence(bridge):
    """重启模拟器：停 → 启 → 初始化 BAAS → 启动游戏，按序执行并返回 True"""
    calls: list[str] = []

    def _stop():
        calls.append("stop")

    def _start():
        calls.append("start")
        return "127.0.0.1:16384"

    def _create(adb):
        calls.append(f"create:{adb}")

    def _launch():
        calls.append("launch")

    bridge.stop = _stop
    bridge.start_simulator = _start
    bridge.create_baas = _create
    bridge.launch_game = _launch
    assert bridge.restart_simulator() is True
    assert calls == ["stop", "start", "create:127.0.0.1:16384", "launch"]


def test_restart_simulator_recovers_when_stop_raises(bridge):
    """stop 异常不应阻断重启流程（继续启动模拟器）"""
    def _stop():
        raise RuntimeError("stop 失败")

    bridge.stop = _stop
    bridge.start_simulator = lambda: "127.0.0.1:16384"
    bridge.create_baas = lambda adb: None
    bridge.launch_game = lambda: None
    assert bridge.restart_simulator() is True


def test_restart_simulator_returns_false_on_failure(bridge):
    """重启中途失败（模拟器启动失败）→ 返回 False，不抛异常"""
    bridge.stop = lambda: None

    def _start():
        raise RuntimeError("模拟器启动失败")

    bridge.start_simulator = _start
    assert bridge.restart_simulator() is False


def test_solve_resets_next_time_before_task(bridge, monkeypatch):
    """solve() 前重置 next_time（对齐 BAAS 官方调度器），任务内按需再设置"""
    bridge.create_baas()
    seen = {}

    def fake_solve(task):
        # BAAS 视角：current_task 执行时 next_time 应为 0
        seen["before"] = bridge.baas_thread.next_time
        bridge.baas_thread.next_time = 55  # arena 票数>1：打一场后设置冷却
        return True

    monkeypatch.setattr(bridge.baas_thread, "solve", fake_solve)
    bridge.solve("arena")
    assert seen["before"] == 0
    assert bridge.last_next_time == 55


def test_solve_does_not_leak_stale_next_time(bridge, monkeypatch):
    """arena 最后一票打完不设置 next_time：不应残留上一场遗留的 55s（第 6 场回归）

    竞技场每天固定 5 张挑战券，不存在第 6 场。旧实现直接调 baas_thread.solve()
    绕过 BAAS 调度器的 next_time 重置，导致最后一场打完 next_time 仍为 55，
    引擎误判「还有票」而白等一个冷却周期并派发第 6 场。
    """
    bridge.create_baas()
    bridge.baas_thread.next_time = 55  # 上一场遗留的陈旧冷却值

    def fake_solve(task):
        return True  # 最后一票：只领奖励返回，不触碰 next_time

    monkeypatch.setattr(bridge.baas_thread, "solve", fake_solve)
    bridge.solve("arena")
    assert bridge.last_next_time == 0

# ---- 游戏内切换账号（switch_game_account） ----


class FakeU2ForSwitch:
    """记录 app_stop/app_start 调用的假 u2 连接"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def app_stop(self, pkg):
        self.calls.append(("app_stop", pkg))

    def app_start(self, pkg, activity=None):
        self.calls.append(("app_start", pkg))


# 切换账号流程三个 OCR 识别区域（与 baas_bridge 顶层常量一致，720p 基准）
_ENTER_REGION = (500, 385, 900, 465)  # Yostar 选择框「进入游戏」按钮
_SWITCH_REGION = (20, 650, 180, 720)  # 登录界面左下角「切换账号」按钮
_TITLE_REGION = (450, 600, 830, 650)  # 标题页底部横幅
_REGIONS = {"ENTER": _ENTER_REGION, "SWITCH": _SWITCH_REGION, "TITLE": _TITLE_REGION}


class FakeSwitchBaasThread:
    """切换账号流程用的假 Baas_thread：点击/按区域 OCR/to_main_page 全记录

    ocr_texts 按识别区域给文本序列：{区域 (x1,y1,x2,y2): [文本,...]}。
    每个区域独立游标，用尽后保持最后一个值。区域 → OCR 调用的映射靠
    _ocr_crop 裁剪放大后的 origin_image 尺寸反推（enlarge 固定 2，
    三个识别区域放大后尺寸互不相同）。
    """

    ENLARGE = 2  # 与 _ocr_crop 默认 enlarge 一致

    def __init__(self, ocr_texts: dict, img):
        self._texts_by_shape: dict[tuple[int, int], list[str]] = {}
        for region, texts in ocr_texts.items():
            x1, y1, x2, y2 = region
            shape = ((y2 - y1) * self.ENLARGE, (x2 - x1) * self.ENLARGE)
            self._texts_by_shape[shape] = list(texts)
        self.ocr = self
        self.latest_img_array = img
        self.ratio = 1.0
        self.u2 = FakeU2ForSwitch()
        self.clicks: list[tuple[int, int]] = []
        self.to_main_page_calls = 0
        self.refreshes = 0

    def ocr_for_single_line(self, **kwargs):
        img = kwargs.get("origin_image")
        if img is None:
            return ""
        texts = self._texts_by_shape.get(tuple(img.shape[:2]))
        if not texts:
            return ""
        if len(texts) > 1:
            return texts.pop(0)
        return texts[0]  # 用尽后保持最后一个

    def update_screenshot_array(self):
        self.refreshes += 1

    def click(self, x, y):
        self.clicks.append((x, y))

    def to_main_page(self):
        self.to_main_page_calls += 1


def _switch_bridge(bridge, sw_kwargs=None):
    """构造 game_package_name 已设置的 bridge（fixture bridge + 切换配置）"""
    bridge.config.baas.game_package_name = "com.custom.bluearchive"
    if sw_kwargs:
        from baas_plus.config import AccountSwitchConfig

        bridge.account_switch = AccountSwitchConfig(**sw_kwargs)
    return bridge


class _FakeClock:
    """可控时钟：sleep 推进时间，配合切换流程的重启/超时逻辑测试"""

    def __init__(self):
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fast_clock(monkeypatch):
    """把 baas_bridge 的 time 模块替换为可控时钟（sleep 推进 time）"""
    clock = _FakeClock()
    monkeypatch.setattr("baas_plus.baas_bridge.time", clock)
    return clock


def test_switch_game_account_full_sequence(bridge, no_sleep):
    """完整切换流程：重启 BA → 登录界面 → 切换账号 → 确定（验证登出）→ 等选择框 → 下拉框 → 第 2 个账号 → 进入游戏"""
    baas = FakeSwitchBaasThread(
        # 第 1 轮：选择框未弹出（ENTER 落空）→ 识别到「切换账号」→ 登出；
        # 登出验证：横幅出现（TITLE）+ 按钮落空（SWITCH 第 2 次读取）；
        # 第 2 轮：ENTER 命中「进入游戏」
        ocr_texts={
            _ENTER_REGION: ["", "进入游戏"],
            _SWITCH_REGION: ["切换账号", ""],
            _TITLE_REGION: ["点击任意区域进入游戏"],
        },
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(2)
    # 1. 强杀并重启 BA 游戏进程（到登录界面）
    assert baas.u2.calls == [
        ("app_stop", "com.custom.bluearchive"),
        ("app_start", "com.custom.bluearchive"),
    ]
    # 2. 切换账号(60,688) → 确定(736,415) → 下拉框(855,315) → 第 2 个账号(600,520) → 进入游戏(642,430)
    assert baas.clicks == [(60, 688), (736, 415), (855, 315), (600, 520), (642, 430)]
    # 3. 进入游戏后回主界面
    assert baas.to_main_page_calls == 1


def test_switch_game_account_skips_logout_when_dialog_direct(bridge, no_sleep):
    """重启后 Yostar 选择框直接弹出（OCR 到「进入游戏」）：跳过切换账号/确定，直接点选"""
    # 第 1 轮三个区域都识别不到，第 2 轮 ENTER 命中「进入游戏」
    baas = FakeSwitchBaasThread(
        ocr_texts={_ENTER_REGION: ["", "进入游戏"], _SWITCH_REGION: [""], _TITLE_REGION: [""]},
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(1)
    # 没有「切换账号」「确定」「点空白」步骤；第 1 个账号 y=420
    assert baas.clicks == [(855, 315), (600, 420), (642, 430)]


def test_switch_game_account_clicks_blank_until_dialog(bridge, no_sleep):
    """登出过渡页：点空白处唤出选择框，OCR 到后立即停手（未弹出不重复点）"""
    baas = FakeSwitchBaasThread(
        # 第 1 轮：登录界面 → 登出（验证：横幅出现 + 按钮落空）；
        # 第 2 轮：ENTER 落空、TITLE 命中横幅 → 点空白一次；
        # 第 3 轮：ENTER 命中「进入游戏」
        ocr_texts={
            _ENTER_REGION: ["", "", "进入游戏"],
            _SWITCH_REGION: ["切换账号", ""],
            _TITLE_REGION: ["点击任意区域进入游戏"],
        },
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(3)
    # 确认登出后第 2 轮点空白(640,360)一次；第 3 轮 OCR 到后不再点击
    assert baas.clicks == [
        (60, 688), (736, 415),
        (640, 360),
        (855, 315), (600, 620),  # 第 3 个账号 = first.y + 2×行高
        (642, 430),
    ]


def test_switch_game_account_never_blank_clicks_before_logout(bridge, no_sleep):
    """安全顺序回归：登出完成前绝不点空白（标题页点空白 = 自动登录旧账号）

    第 1 轮识别到标题横幅但「切换账号」按钮 OCR 抖动漏识别 → 不点任何东西；
    第 2 轮重新识别到「切换账号」才走登出流程。
    """
    baas = FakeSwitchBaasThread(
        ocr_texts={
            _ENTER_REGION: ["", "", "进入游戏"],
            _SWITCH_REGION: ["", "切换账号", ""],
            _TITLE_REGION: ["点击任意区域进入游戏"],
        },
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(1)
    # 全程没有 (640,360) 的空白点击
    assert baas.clicks == [(60, 688), (736, 415), (855, 315), (600, 420), (642, 430)]


def test_switch_game_account_retries_logout_when_confirm_missed(bridge, fast_clock):
    """点「确定」后未检测到登出（点空）：自动重试「切换账号 → 确定」序列"""
    baas = FakeSwitchBaasThread(
        # 第 1 次登出验证：10 次轮询 TITLE 全落空 → 失败重试；
        # 第 2 次验证：横幅出现 + 按钮落空；随后 ENTER 命中「进入游戏」
        ocr_texts={
            _ENTER_REGION: ["", "进入游戏"],
            _SWITCH_REGION: ["切换账号", ""],
            _TITLE_REGION: [""] * 10 + ["点击任意区域进入游戏"],
        },
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(1)
    # 「切换账号 → 确定」被执行两次
    assert baas.clicks == [
        (60, 688), (736, 415),
        (60, 688), (736, 415),
        (855, 315), (600, 420), (642, 430),
    ]


def test_switch_game_account_restarts_on_auto_login(bridge, fast_clock):
    """自动登录复位：重启后识别不到任何特征（疑似直接进了主界面）→ 再次杀进程重启后恢复"""
    # 重启前 3 轮全部落空（盲等 4s 触发重启），重启后第 1 轮识别到选择框
    baas = FakeSwitchBaasThread(
        ocr_texts={
            _ENTER_REGION: ["", "", "", "进入游戏"],
            _SWITCH_REGION: ["", "", ""],
            _TITLE_REGION: ["", "", ""],
        },
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge, sw_kwargs={"app_start_timeout": 600.0, "blind_restart_after": 4.0})
    bridge.baas_thread = baas
    bridge.switch_game_account(1)
    # 初始重启 + 1 次自动登录复位重启
    assert baas.u2.calls == [
        ("app_stop", "com.custom.bluearchive"),
        ("app_start", "com.custom.bluearchive"),
        ("app_stop", "com.custom.bluearchive"),
        ("app_start", "com.custom.bluearchive"),
    ]
    # 复位成功后正常选号；全程没有空白点击（未登出状态）
    assert baas.clicks == [(855, 315), (600, 420), (642, 430)]


def test_switch_game_account_restart_cap(bridge, fast_clock):
    """自动登录复位有次数上限：连续盲等只重启 _SWITCH_MAX_RESTARTS 次，最终抛超时错误"""
    baas = FakeSwitchBaasThread(
        ocr_texts={_ENTER_REGION: [""], _SWITCH_REGION: [""], _TITLE_REGION: [""]},
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge, sw_kwargs={"app_start_timeout": 600.0, "blind_restart_after": 4.0})
    bridge.baas_thread = baas
    with pytest.raises(RuntimeError, match="等待登录界面超时"):
        bridge.switch_game_account(1)
    # 初始重启 + 2 次复位重启 = 3 组 stop/start；期间没有任何点击
    assert baas.u2.calls == [("app_stop", "com.custom.bluearchive"), ("app_start", "com.custom.bluearchive")] * 3
    assert baas.clicks == []


def test_switch_game_account_rejects_invalid_index(bridge, no_sleep):
    """序号 < 1：不执行任何点击/OFF 操作，直接抛错"""
    baas = FakeSwitchBaasThread(
        ocr_texts={_ENTER_REGION: ["切换账号"], _SWITCH_REGION: [""], _TITLE_REGION: [""]},
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    with pytest.raises(RuntimeError, match="序号不合法"):
        bridge.switch_game_account(0)
    assert baas.u2.calls == []
    assert baas.clicks == []


@pytest.mark.parametrize(
    "texts,match_text",
    [
        ({"ENTER": ["不知道"], "SWITCH": [""], "TITLE": [""]}, "等待登录界面超时"),
        # 已完成登出但选择框一直没出现：报 Yostar 选择框超时（指引不同）
        (
            {"ENTER": ["", ""], "SWITCH": ["切换账号", ""], "TITLE": ["点击任意区域进入游戏"]},
            "等待 Yostar 账号选择框超时",
        ),
    ],
)
def test_switch_game_account_timeout_raises(bridge, no_sleep, texts, match_text):
    """OCR 一直识别不到目标按钮：超时抛 RuntimeError（带排查指引）"""
    texts = {_REGIONS[v]: t for v, t in texts.items()}
    baas = FakeSwitchBaasThread(ocr_texts=texts, img=_blank_screen())
    bridge = _switch_bridge(bridge, sw_kwargs={"app_start_timeout": 0.2, "dialog_timeout": 0.2})
    bridge.baas_thread = baas
    with pytest.raises(RuntimeError, match=match_text):
        bridge.switch_game_account(1)


def test_switch_game_account_logout_verify_never_succeeds(bridge, fast_clock):
    """反复点「切换账号 → 确定」始终检测不到登出：抛切换失败错误（不盲点空白）"""
    baas = FakeSwitchBaasThread(
        ocr_texts={_ENTER_REGION: [""], _SWITCH_REGION: ["切换账号", ""], _TITLE_REGION: [""]},
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge, sw_kwargs={"app_start_timeout": 600.0})
    bridge.baas_thread = baas
    with pytest.raises(RuntimeError, match="切换账号失败"):
        bridge.switch_game_account(1)
    # 只有点击「切换账号/确定」的重试，没有空白点击（防自动登录）
    assert set(baas.clicks) == {(60, 688), (736, 415)}


def test_switch_game_account_requires_baas_thread(bridge):
    """Baas_thread 未初始化：直接抛 RuntimeError"""
    bridge = _switch_bridge(bridge)
    with pytest.raises(RuntimeError, match="Baas_thread 未初始化"):
        bridge.switch_game_account(1)


def test_switch_game_account_rejects_invalid_index_even_with_blank_tail_spaces(bridge, no_sleep):
    """尾号只有空白字符：视为未设置，序号仍须 >= 1"""
    baas = FakeSwitchBaasThread(
        ocr_texts={_ENTER_REGION: [""], _SWITCH_REGION: [""], _TITLE_REGION: [""]},
        img=_blank_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    with pytest.raises(RuntimeError, match="序号不合法"):
        bridge.switch_game_account(0, "   ")
    assert baas.clicks == []


# ---- 切换账号：手机尾号 OCR 匹配（列表顺序不固定，不按序号点选） ----


def _num_region(first_pos=(600, 420)):
    """第 n 个账号条目的手机号 OCR 区域（由 _ACCOUNT_NUMBER_OCR_OFFSET 推算）。

    注意所有条目区域裁剪放大后**同形**（同为 30px 高），FakeSwitchBaasThread
    只能按 shape 出队——而代码按条目序从上往下逐条扫描，序列顺序即条目顺序。
    """
    from baas_plus.baas_bridge import _ACCOUNT_NUMBER_OCR_OFFSET

    x, y = first_pos
    dx1, dy1, dx2, dy2 = _ACCOUNT_NUMBER_OCR_OFFSET
    return (x + dx1, y + dy1, x + dx2, y + dy2)


def _yostar_card_screen():
    """模拟 Yostar 选择框截图：卡片区域纯白，其余为黑（背景/版权文字）"""
    import numpy as np

    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    img[155:566, 262:1110] = 255  # 白色卡片（实测 4.png/5.png 卡片范围）
    return img


def test_switch_game_account_selects_by_phone_tail(bridge, no_sleep):
    """设置手机尾号：OCR 逐条匹配号码尾号点选，不依赖列表顺序"""
    baas = FakeSwitchBaasThread(
        ocr_texts={
            _ENTER_REGION: ["进入游戏"],
            # 条目 1 是 3522、条目 2 是 9524（用尽后保持最后一个值）
            _num_region(): ["136****3522", "135****9524", "135****9524"],
        },
        img=_yostar_card_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(1, "9524")
    # 尾号 9524 命中第 2 个条目 (600,520)；不走序号定位（序号 1 = 第 1 项 y=420）
    assert baas.clicks == [(855, 315), (600, 520), (642, 430)]


def test_switch_game_account_tail_not_found_raises(bridge, no_sleep):
    """尾号未匹配到任何条目：重扫一轮后抛错，且不点击条目/「进入游戏」"""
    baas = FakeSwitchBaasThread(
        ocr_texts={
            _ENTER_REGION: ["进入游戏"],
            _num_region(): ["136****3522", "135****9524", "135****9524"],
        },
        img=_yostar_card_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    with pytest.raises(RuntimeError, match="手机尾号为 1111 的条目"):
        bridge.switch_game_account(1, "1111")
    # 只有展开下拉列表的点击；选号失败绝不点「进入游戏」（防止进错号）
    assert baas.clicks == [(855, 315)]


def test_switch_game_account_tail_retries_after_ocr_jitter(bridge, no_sleep):
    """第一轮 OCR 漏读（返回空）：整轮扫完自动重扫第二轮并命中"""
    baas = FakeSwitchBaasThread(
        ocr_texts={
            _ENTER_REGION: ["进入游戏"],
            # 第 1 轮：条目 1 漏读、条目 2 读到 3522（不匹配）；第 2 轮：条目 1 读到 9524
            _num_region(): ["", "136****3522", "135****9524"],
        },
        img=_yostar_card_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(1, "9524")
    assert baas.clicks == [(855, 315), (600, 420), (642, 430)]


def test_switch_game_account_stops_scan_outside_card(bridge, no_sleep):
    """扫描超出 Yostar 白色卡片即停止：不会把卡片下方背景/版权文字当号码

    卡片只容纳 2 个条目（第 3 个条目点击 y=620 在卡片外）——若继续扫描，
    背景区域 OCR 会继续消费 fake 序列并可能误命中/点错位置。
    """
    baas = FakeSwitchBaasThread(
        ocr_texts={
            _ENTER_REGION: ["进入游戏"],
            # 只有 2 个真实条目；若扫到第 3 个条目会消费到背景序列（不匹配）
            _num_region(): ["136****3522", "135****9524", ""],
        },
        img=_yostar_card_screen(),
    )
    bridge = _switch_bridge(bridge)
    bridge.baas_thread = baas
    bridge.switch_game_account(2, "9524")
    assert baas.clicks == [(855, 315), (600, 520), (642, 430)]



def test_stop_can_keep_simulator_running(bridge, monkeypatch):
    """stop(stop_simulator=False)：停止 BAAS 线程但保留模拟器（切换模式传机用）"""
    from baas_plus.baas_bridge import BaasBridge

    stopped = []
    monkeypatch.setattr(BaasBridge, "_stop_simulator", lambda self: stopped.append(True))
    bridge.stop(stop_simulator=False)
    assert bridge.baas_thread is None
    assert stopped == []  # 模拟器未关
    bridge.stop(stop_simulator=True)
    assert stopped == [True]

