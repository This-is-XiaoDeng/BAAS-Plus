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

        def get(self, key, default=None):
            return "官服" if key == "server" else default

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
