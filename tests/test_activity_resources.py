"""活动资源检查/注入（当前服缺截图模板时的内存注入）与轮播图匹配判定测试

不依赖真实 BAAS：用临时目录搭一棵"假 BAAS 源码树"（module/activities、
explore_task_data、src/images/<服>/...），并 monkeypatch `_activity_resource_root`。
需要 cv2 的用例在缺失时跳过（CI 单独装 opencv-python-headless）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from baas_plus import activity_assets as aa
from baas_plus.baas_bridge import BaasBridge
from baas_plus.config import AppConfig

MODULE = "AHundredYearsofOneFlowerLetsGetRealwithaWaterBattle"
BOXES = {
    "enter1": (1157, 168, 1225, 222),
    "enter2": (92, 146, 124, 187),
    "enter3": (150, 526, 263, 556),
}


def _xyrange_text(boxes=BOXES) -> str:
    lines = "\n".join(f"    '{k}': {tuple(v)}," for k, v in boxes.items())
    return f'prefix = "activity"\npath = "activity/{MODULE}"\nx_y_range = {{\n{lines}\n}}\n'


def make_fake_baas_root(root: Path, *, with_module: bool = True, with_json: bool = True,
                        jp_xyrange: bool = True, cn_modules=("OtherModule",)) -> Path:
    """搭一棵假 BAAS 源码树：当前服（CN）只有 OtherModule，目标模块只在日服

    模块代码与关卡数据是全服共享的，所以 cn_modules 也会有（只有当前服的截图
    资源不同），与 BAAS 上游的实际情况一致。
    """
    (root / "module" / "activities").mkdir(parents=True, exist_ok=True)
    (root / "src" / "explore_task_data" / "activities").mkdir(parents=True, exist_ok=True)
    (root / "src" / "images" / "CN" / "x_y_range" / "activity").mkdir(parents=True, exist_ok=True)
    (root / "src" / "images" / "CN" / "activity").mkdir(parents=True, exist_ok=True)
    for name in cn_modules:
        (root / "src" / "images" / "CN" / "x_y_range" / "activity" / f"{name}.py").write_text(
            _xyrange_text(), encoding="utf-8"
        )
        (root / "src" / "explore_task_data" / "activities" / f"{name}.json").write_text("{}", encoding="utf-8")
        (root / "module" / "activities" / f"{name}.py").write_text(
            "def sweep(self):\n    return True\n", encoding="utf-8"
        )
        tpl_dir = root / "src" / "images" / "CN" / "activity" / name
        tpl_dir.mkdir(parents=True, exist_ok=True)
        (tpl_dir / "enter1.png").write_bytes(b"png")
    if with_module:
        (root / "module" / "activities" / f"{MODULE}.py").write_text(
            "def sweep(self):\n    return True\n", encoding="utf-8"
        )
    if with_json:
        (root / "src" / "explore_task_data" / "activities" / f"{MODULE}.json").write_text("{}", encoding="utf-8")
    if jp_xyrange:
        (root / "src" / "images" / "JP" / "x_y_range" / "activity").mkdir(parents=True, exist_ok=True)
        (root / "src" / "images" / "JP" / "x_y_range" / "activity" / f"{MODULE}.py").write_text(
            _xyrange_text(), encoding="utf-8"
        )
    return root


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch):
    cfg = AppConfig()
    cfg.accounts[0].baas.server = "cn"
    cfg.accounts[0].baas.current_activity = MODULE
    br = BaasBridge(cfg, data_dir=tmp_path / "data")
    root = make_fake_baas_root(tmp_path / "baas")
    monkeypatch.setattr(br, "_activity_resource_root", lambda: str(root))
    return br


# ---- 报告分级 ----


def test_report_lists_missing_server_assets_and_borrowed_boxes(bridge):
    rep = bridge.activity_resource_report()
    assert rep["module"] == MODULE
    assert rep["identifier"] == "CN"
    assert rep["code_module"] is True and rep["stage_json"] is True
    assert rep["server_xyrange"] is False and rep["whitelisted"] is False
    codes = [i["code"] for i in rep["issues"]]
    assert codes == ["server_assets_missing"]
    # 坐标框可沿用同活动其它服（日服）
    assert rep["boxes"]["enter1"] == list(BOXES["enter1"])
    assert rep["boxes_source"] == "other_server"
    assert rep["ready"] is False
    assert "上传" in rep["hint"]


def test_report_whitelisted_module_is_ready(bridge):
    rep = bridge.activity_resource_report("OtherModule")
    assert rep["whitelisted"] is True and rep["ready"] is True
    assert rep["issues"] == []


def test_report_grades_outdated_baas(tmp_path: Path, monkeypatch):
    cfg = AppConfig()
    br = BaasBridge(cfg, data_dir=tmp_path / "data")
    root = make_fake_baas_root(tmp_path / "baas", with_module=False)
    monkeypatch.setattr(br, "_activity_resource_root", lambda: str(root))
    rep = br.activity_resource_report(MODULE)
    assert [i["code"] for i in rep["issues"]] == ["code_module_missing"]
    assert "升级 BAAS" in rep["hint"]


def test_report_without_baas_is_graceful(tmp_path: Path, monkeypatch):
    br = BaasBridge(AppConfig(), data_dir=tmp_path / "data")
    monkeypatch.setattr(br, "_activity_resource_root", lambda: None)
    rep = br.activity_resource_report(MODULE)
    assert [i["code"] for i in rep["issues"]] == ["baas_unavailable"]
    assert rep["ready"] is False


def test_report_without_module_name(tmp_path: Path):
    br = BaasBridge(AppConfig(), data_dir=tmp_path / "data")
    rep = br.activity_resource_report()
    assert [i["code"] for i in rep["issues"]] == ["no_module"]


# ---- 服标识（修复国际服写死 Global 的旧映射）----


def test_server_identifier_uses_thread_value(tmp_path: Path, monkeypatch):
    cfg = AppConfig()
    cfg.accounts[0].baas.server = "in"
    br = BaasBridge(cfg, data_dir=tmp_path)
    root = tmp_path / "baas"
    (root / "src" / "images" / "Global_ko-kr").mkdir(parents=True)
    assert br._server_identifier(str(root)) == "Global_ko-kr"
    class T:
        identifier = "Global_en-us"
    br.baas_thread = T()
    assert br._server_identifier(str(root)) == "Global_en-us"


def test_server_identifier_falls_back_when_no_global_dir(tmp_path: Path):
    cfg = AppConfig()
    cfg.accounts[0].baas.server = "in"
    br = BaasBridge(cfg, data_dir=tmp_path)
    assert br._server_identifier(str(tmp_path / "nope")) == "Global"


# ---- 可用性与注入 ----


def test_ensure_activity_resources_whitelisted(bridge):
    ok, why = bridge.ensure_activity_resources("OtherModule")
    assert ok and "自带" in why


def test_ensure_resources_needs_toggle_when_patch_ready(bridge):
    """备好本地资源但没开开关 → 明确提示去 WebUI 开启（而不是运行时含糊报错）"""
    _install_patch(bridge)
    ok, why = bridge.ensure_activity_resources(MODULE)
    assert ok is False
    assert "活动策略" in why and "开启" in why


def test_ensure_resources_injects_when_enabled(bridge, monkeypatch):
    bridge.config.accounts[0].activity.inject_activity_resources = True
    _install_patch(bridge)
    calls = {}

    def fake_inject(module):
        calls["module"] = module
        return {"ok": True, "injected": ["enter1"], "identifier": "CN"}

    monkeypatch.setattr(bridge, "inject_activity_resources", fake_inject)
    ok, why = bridge.ensure_activity_resources(MODULE)
    assert ok is True
    assert calls["module"] == MODULE
    assert "已注入" in why
    assert bridge.activity_module_available(MODULE) is True


def test_ensure_resources_patch_missing_hint(bridge):
    bridge.config.accounts[0].activity.inject_activity_resources = True
    ok, why = bridge.ensure_activity_resources(MODULE)
    assert ok is False
    assert "上传" in why or "检查" in why


def test_inject_writes_baas_registries(tmp_path: Path, monkeypatch):
    """注入内容与 BAAS init_image_data 等价：image_dic/activity_enter1 + 坐标框"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    import sys
    import types

    cfg = AppConfig()
    cfg.accounts[0].baas.current_activity = MODULE
    br = BaasBridge(cfg, data_dir=tmp_path / "data")
    root = make_fake_baas_root(tmp_path / "baas")
    monkeypatch.setattr(br, "_activity_resource_root", lambda: str(root))
    _install_patch(br)

    position = types.ModuleType("core.position")
    position.image_dic = {}
    position.image_x_y_range = {}
    core = types.ModuleType("core")
    core.position = position
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.position", position)
    monkeypatch.setattr("baas_plus.baas_bridge.import_baas", lambda repo_dir="": (None, None, None))

    result = br.inject_activity_resources(MODULE)
    assert result["ok"] is True and result["injected"] == ["enter1"]
    assert "activity_enter1" in position.image_dic["CN"]
    assert position.image_x_y_range["CN"]["activity"]["enter1"] == BOXES["enter1"]
    assert position.image_dic["CN"]["activity_enter1"].shape[:2] == (54, 68)


# ---- 模板路径优先本地资源库 ----


def test_template_path_prefers_local_patch(bridge):
    _install_patch(bridge)
    path = bridge._activity_template_path(MODULE, "enter1.png")
    assert path and path.startswith(str(aa.patch_root(bridge._activity_data_dir())))


def test_template_path_none_when_nowhere(bridge):
    assert bridge._activity_template_path(MODULE, "enter1.png") is None


# ---- 轮播图匹配判定（滑窗 + 框内双口径）----


class FakeThreadWithFrame:
    def __init__(self, img):
        self.latest_img_array = img
        self.ratio = 1.0
        self.identifier = "CN"
        self.shots = 0

    def update_screenshot_array(self):
        self.shots += 1


def _screen_with_banner():
    """合成主页：噪声背景 + 轮播图区域 (1109,133,1280,281) 内的高特征画面"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    rng = np.random.default_rng(7)
    screen = rng.integers(0, 60, (720, 1280, 3), dtype=np.uint8)
    x1, y1, x2, y2 = BOXES["enter1"]
    cv2.rectangle(screen, (x1, y1), (x2, y2), (170, 150, 90), -1)
    cv2.circle(screen, ((x1 + x2) // 2, (y1 + y2) // 2), 14, (30, 60, 230), -1)
    cv2.putText(screen, "F", (x1 + 8, y2 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return screen


def _write_template(path: Path, img) -> str:
    cv2 = pytest.importorskip("cv2")
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return str(path)


def test_match_banner_uses_box_route(bridge, tmp_path, monkeypatch):
    """模板与声明框内像素一致（BAAS 口径）→ 命中"""
    screen = _screen_with_banner()
    box = BOXES["enter1"]
    tpl = screen[box[1]:box[3], box[0]:box[2]].copy()
    tpl_path = _write_template(tmp_path / "tpl.png", tpl)
    monkeypatch.setattr(bridge, "_activity_template_path", lambda mod, fname: tpl_path)
    monkeypatch.setattr(bridge, "read_activity_boxes", lambda mod: {"enter1": list(box)})
    bridge.baas_thread = FakeThreadWithFrame(screen)
    assert bridge.match_banner_activity([MODULE]) == MODULE
    assert bridge.baas_thread.shots == 1


def test_match_banner_small_template_needs_high_score(bridge, tmp_path, monkeypatch):
    """小模板（面积 < MIN_TRUSTED_TEMPLATE_AREA）必须达到小模板严格阈值

    回归背景：20x10 / 22x20 这类小模板的 TM_CCOEFF_NORMED 会虚高——实测在完全
    无关的轮播图上也刷到 0.80~0.91（国服 GetSetGoKivotosHaloGames 0.8038、日服
    MoonlightDreams 0.913），旧逻辑据此误判"轮播图就是目标活动"。
    """
    screen = _screen_with_banner()
    tpl = screen[180:200, 1180:1200].copy()  # 20x20，面积 400
    tpl_path = _write_template(tmp_path / "small.png", tpl)
    monkeypatch.setattr(bridge, "_activity_template_path", lambda mod, fname: tpl_path)
    monkeypatch.setattr(bridge, "read_activity_boxes", lambda mod: {})
    bridge.baas_thread = FakeThreadWithFrame(screen)
    assert aa.template_area(tpl) < aa.MIN_TRUSTED_TEMPLATE_AREA
    assert aa.score_slide(screen[133:281, 1109:1280], tpl) >= aa.SMALL_TEMPLATE_MIN_SCORE
    # 抬高小模板阈值 → 同一张模板被拒，说明拒绝确实来自该约束
    monkeypatch.setattr(aa, "SMALL_TEMPLATE_MIN_SCORE", 1.01)
    assert bridge.match_banner_activity([MODULE]) is None


def test_match_banner_rejects_small_template_false_positive(bridge, tmp_path, monkeypatch):
    """小模板滑窗分落在 [0.80, 0.95) 的历史假阳性区间时必须拒绝"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    screen = _screen_with_banner()
    banner = screen[133:281, 1109:1280]
    base = screen[180:200, 1180:1200].copy()  # 20x20
    rng = np.random.default_rng(5)
    noise = rng.integers(0, 255, base.shape, dtype=np.uint8)
    band = None
    for alpha in (0.3, 0.45, 0.55, 0.65, 0.75, 0.85, 0.9):
        cand = cv2.addWeighted(base, alpha, noise, 1 - alpha, 0)
        value = aa.score_slide(banner, cand)
        if 0.80 <= value < aa.SMALL_TEMPLATE_MIN_SCORE:
            band = cand
            break
    if band is None:  # pragma: no cover - 合成画面下极少发生
        pytest.skip("未能构造出落在假阳性区间的合成模板")
    tpl_path = _write_template(tmp_path / "fp.png", band)
    monkeypatch.setattr(bridge, "_activity_template_path", lambda mod, fname: tpl_path)
    monkeypatch.setattr(bridge, "read_activity_boxes", lambda mod: {})
    bridge.baas_thread = FakeThreadWithFrame(screen)
    assert bridge.match_banner_activity([MODULE]) is None


def test_match_banner_slide_route_for_large_template(bridge, tmp_path, monkeypatch):
    """大模板（≥1500 面积）偏移一点仍靠滑窗命中（历史宽容路径保留）"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    screen = _screen_with_banner()
    box = BOXES["enter1"]
    tpl = screen[box[1]:box[3], box[0]:box[2]].copy()
    # 让模板与画面略有差异（模拟同一皮肤的不同帧），框内 1:1 分数下降
    tpl = cv2.addWeighted(tpl, 0.9, np.full_like(tpl, 40), 0.1, 0)
    tpl_path = _write_template(tmp_path / "tpl2.png", tpl)
    monkeypatch.setattr(bridge, "_activity_template_path", lambda mod, fname: tpl_path)
    monkeypatch.setattr(bridge, "read_activity_boxes", lambda mod: {})
    bridge.baas_thread = FakeThreadWithFrame(screen)
    assert aa.template_area(tpl) >= aa.MIN_TRUSTED_TEMPLATE_AREA
    assert bridge.match_banner_activity([MODULE]) == MODULE


# ---- 上传补齐（配置时）----


def test_install_activity_assets_from_upload(bridge):
    cv2 = pytest.importorskip("cv2")
    screen = _screen_with_banner()
    ok, buf = cv2.imencode(".png", screen)
    assert ok
    result = bridge.install_activity_assets(buf.tobytes(), module=MODULE, kind="main")
    assert result["ok"] is True
    assert result["stored"]["saved"] == ["enter1"]
    assert result["report"]["ready"] is True
    assert bridge.activity_module_available(MODULE) is True
    assert (aa.patch_root(bridge._activity_data_dir()) / "CN" / MODULE / "enter1.png").is_file()


def test_install_activity_assets_rejects_unknown_kind(bridge):
    result = bridge.install_activity_assets(b"x", module=MODULE, kind="weird")
    assert result["ok"] is False and "未知" in result["reason"]


def test_install_activity_assets_without_boxes(tmp_path: Path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    cfg = AppConfig()
    cfg.accounts[0].baas.current_activity = MODULE
    br = BaasBridge(cfg, data_dir=tmp_path / "data")
    root = make_fake_baas_root(tmp_path / "baas", jp_xyrange=False)
    monkeypatch.setattr(br, "_activity_resource_root", lambda: str(root))
    screen = _screen_with_banner()
    ok, buf = cv2.imencode(".png", screen)
    result = br.install_activity_assets(buf.tobytes(), module=MODULE, kind="main")
    assert result["ok"] is False and "坐标框" in result["reason"]


# ---- 配置扫描 ----


def test_list_patched_modules(bridge):
    _install_patch(bridge)
    assert bridge.list_patched_modules() == [MODULE]


def _install_patch(br: BaasBridge) -> None:
    """往本地资源库塞一份"已补齐"的 enter1（内容无关，只验证流程）"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    rng = np.random.default_rng(3)
    img = rng.integers(0, 255, (54, 68, 3), dtype=np.uint8)
    d = aa.patch_root(br._activity_data_dir()) / "CN" / MODULE
    d.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(d / "enter1.png"), img)
    (d / "meta.json").write_text(
        json.dumps({"module": MODULE, "identifier": "CN", "boxes": {k: list(v) for k, v in BOXES.items()}}),
        encoding="utf-8",
    )
