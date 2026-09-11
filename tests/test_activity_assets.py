"""活动资源库（本地补齐当前服活动截图模板）测试

不依赖真实 BAAS / 模拟器 / 网络：坐标框解析用临时文件，裁剪与打分校验用
合成画面（需要 cv2，缺失时跳过——CI 单独安装 opencv-python-headless）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from baas_plus import activity_assets as aa

pytest.importorskip("cv2")
pytest.importorskip("numpy")


def _frame_with_pattern(box=(1157, 168, 1225, 222), size=(720, 1280)):
    """合成一张"主页截图"：随机背景 + 目标框内一块高特征图案（size 为 (高, 宽)）"""
    import cv2
    import numpy as np

    rng = np.random.default_rng(2026)
    frame = rng.integers(0, 60, (size[0], size[1], 3), dtype=np.uint8)
    x1, y1, x2, y2 = box
    cv2.rectangle(frame, (x1, y1), (x2, y2), (200, 180, 60), -1)
    cv2.circle(frame, ((x1 + x2) // 2, (y1 + y2) // 2), 12, (40, 40, 220), -1)
    cv2.putText(frame, "W", (x1 + 6, y2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    return frame


# ---- 名字与路径安全 ----


def test_is_safe_name_allows_baas_module_names():
    # BAAS 里真实存在带撇号/数字/下划线的模块名
    assert aa.is_safe_name("AHundredYearsofOneFlowerLetsGetRealwithaWaterBattle")
    assert aa.is_safe_name("InSearchOfAHiddenHeritageTrinity'sExtracurricularActivities")
    assert aa.is_safe_name("no_227_kinosaki_spa")
    assert aa.is_safe_name("JP_2025_06_25")


def test_is_safe_name_rejects_traversal():
    for bad in ("", "../etc", "a/b", "a\\b", "..", ".hidden"):
        assert not aa.is_safe_name(bad), bad
    with pytest.raises(ValueError):
        aa.module_dir("data", "CN", "../../evil")


# ---- 坐标框解析 ----


def test_boxes_from_xyrange_file(tmp_path: Path):
    f = tmp_path / "Mod.py"
    f.write_text(
        'prefix = "activity"\npath = "activity/Mod"\nx_y_range = {\n'
        "    'enter1': (1157, 168, 1225, 222),\n"
        "    'enter2': (92, 146, 124, 187),\n"
        "    'enter3': (150, 526, 263, 556),\n"
        "    'other': (1, 2, 3, 4),\n}\n",
        encoding="utf-8",
    )
    boxes = aa.boxes_from_xyrange_file(f)
    assert boxes["enter1"] == [1157, 168, 1225, 222]
    assert boxes["enter3"] == [150, 526, 263, 556]
    assert "other" not in boxes  # 只取 enter1/2/3


def test_boxes_from_xyrange_file_tolerates_garbage(tmp_path: Path):
    bad = tmp_path / "Bad.py"
    bad.write_text("x_y_range = {oops\n", encoding="utf-8")
    assert aa.boxes_from_xyrange_file(bad) == {}
    assert aa.boxes_from_xyrange_file(tmp_path / "missing.py") == {}


# ---- 裁剪 + 自校验 + 落盘 ----


def test_store_frame_assets_crops_and_verifies(tmp_path: Path):
    frame = _frame_with_pattern()
    boxes = {"enter1": [1157, 168, 1225, 222]}
    result = aa.store_frame_assets(tmp_path, "CN", "Mod", frame, boxes, ("enter1",), "unit")
    assert result["saved"] == ["enter1"]
    # 自裁模板对同一帧的 BAAS 口径相似度应为 1.0（同一块像素）
    assert result["verification"]["enter1"]["score"] == 1.0
    assert result["verification"]["enter1"]["rgb_ok"] is True
    # 落盘 + 元信息
    assert aa.present_assets(tmp_path, "CN", "Mod") == ["enter1"]
    meta = aa.load_meta(tmp_path, "CN", "Mod")
    assert meta["boxes"]["enter1"] == [1157, 168, 1225, 222]
    assert meta["assets"] == ["enter1"]
    assert aa.read_asset(tmp_path, "CN", "Mod", "enter1")


def test_store_frame_assets_scales_box_for_higher_resolution(tmp_path: Path):
    """1920x1080 截图（ratio=1.5）按 1280x720 基准框裁剪"""
    import cv2

    frame = cv2.resize(_frame_with_pattern(), (1920, 1080), interpolation=cv2.INTER_CUBIC)
    boxes = {"enter1": [1157, 168, 1225, 222]}
    result = aa.store_frame_assets(tmp_path, "CN", "Mod", frame, boxes, ("enter1",), "unit")
    assert result["saved"] == ["enter1"]
    assert result["verification"]["enter1"]["frame"] == "1920x1080"
    template = aa.read_asset(tmp_path, "CN", "Mod", "enter1")
    assert template is not None


def test_store_frame_assets_rejects_blank_crop(tmp_path: Path):
    """截图取错区域/纯色时应拒绝，而不是写入一张废模板"""
    import numpy as np

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)  # 全黑
    boxes = {"enter1": [1157, 168, 1225, 222]}
    with pytest.raises(ValueError, match="近似纯色"):
        aa.store_frame_assets(tmp_path, "CN", "Mod", frame, boxes, ("enter1",), "unit")
    assert aa.present_assets(tmp_path, "CN", "Mod") == []


def test_store_frame_assets_marks_menu_assets_unverified(tmp_path: Path):
    frame = _frame_with_pattern()
    boxes = {"enter2": [92, 146, 124, 187], "enter3": [150, 526, 263, 556]}
    result = aa.store_frame_assets(tmp_path, "CN", "Mod", frame, boxes, ("enter2", "enter3"), "unit")
    assert sorted(result["saved"]) == ["enter2", "enter3"]
    assert result["verification"]["enter2"].get("unverified")


def test_delete_patch(tmp_path: Path):
    frame = _frame_with_pattern()
    aa.store_frame_assets(tmp_path, "CN", "Mod", frame, {"enter1": [1157, 168, 1225, 222]}, ("enter1",), "unit")
    assert aa.delete_patch(tmp_path, "CN", "Mod") is True
    assert aa.present_assets(tmp_path, "CN", "Mod") == []
    assert aa.delete_patch(tmp_path, "CN", "Mod") is False


# ---- 打分校验 ----


def test_score_in_box_matches_baas_convention(tmp_path: Path):
    import cv2

    frame = _frame_with_pattern()
    box = [1157, 168, 1225, 222]
    tpl = frame[168:222, 1157:1225].copy()
    good = aa.score_in_box(frame, box, tpl)
    assert good["score"] == 1.0 and good["rgb_ok"]
    # 换一张完全不同的模板：rgb 预筛就应否掉（与 BAAS compare_image_rgb 一致）
    wrong = cv2.imread(str(tmp_path / "none.png")) if False else (tpl * 0 + 255)
    bad = aa.score_in_box(frame, box, wrong)
    assert bad["rgb_ok"] is False and bad["score"] is None


def test_score_slide_and_area():
    frame = _frame_with_pattern()
    tpl = frame[168:222, 1157:1225].copy()
    assert aa.score_slide(frame[133:281, 1109:1280], tpl) == 1.0
    assert aa.template_area(tpl) == 68 * 54
    # 模板比区域大 → 直接 0（不参与滑窗判定）
    assert aa.score_slide(frame[133:180, 1109:1150], tpl) == 0.0
