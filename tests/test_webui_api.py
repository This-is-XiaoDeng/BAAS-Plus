"""WebUI 活动资源 API 测试（配置时检查/补齐，不依赖真实 BAAS）

用 FastAPI TestClient 直接打接口：检查报告、上传截图补齐、删除还原、
保存配置时自动附带资源自检报告。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baas_plus import activity_assets as aa
from baas_plus import config as config_module
from baas_plus.baas_bridge import BaasBridge
from baas_plus.config import AppConfig
from baas_plus.webui.app import create_app

MODULE = "AHundredYearsofOneFlowerLetsGetRealwithaWaterBattle"
BOXES = {
    "enter1": (1157, 168, 1225, 222),
    "enter2": (92, 146, 124, 187),
    "enter3": (150, 526, 263, 556),
}


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path: Path, monkeypatch):
    """PUT /api/config 会 save_config() 到默认路径：重定向到 tmp，绝不碰仓库 data/"""
    target = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(target))
    return target


@pytest.fixture
def client(tmp_path: Path, _isolated_config_path):
    cfg = AppConfig(data_dir=str(tmp_path))
    app = create_app(cfg)
    return TestClient(app)


def _fake_baas_root(root: Path) -> Path:
    (root / "module" / "activities").mkdir(parents=True, exist_ok=True)
    (root / "src" / "explore_task_data" / "activities").mkdir(parents=True, exist_ok=True)
    (root / "src" / "images" / "JP" / "x_y_range" / "activity").mkdir(parents=True, exist_ok=True)
    (root / "module" / "activities" / f"{MODULE}.py").write_text("def sweep(self):\n    return True\n", encoding="utf-8")
    (root / "src" / "explore_task_data" / "activities" / f"{MODULE}.json").write_text("{}", encoding="utf-8")
    lines = "\n".join(f"    '{k}': {tuple(v)}," for k, v in BOXES.items())
    (root / "src" / "images" / "JP" / "x_y_range" / "activity" / f"{MODULE}.py").write_text(
        f'prefix = "activity"\npath = "activity/{MODULE}"\nx_y_range = {{\n{lines}\n}}\n', encoding="utf-8"
    )
    return root


def test_report_without_module(client):
    r = client.get("/api/activity-resources")
    assert r.status_code == 200
    report = r.json()["report"]
    assert [i["code"] for i in report["issues"]] == ["no_module"]


def test_report_without_baas_is_graceful(client, tmp_path):
    r = client.put(
        "/api/config",
        json={
            "data_dir": str(tmp_path),
            "accounts": [
                {
                    "id": "acc_test",
                    "name": "测试账号",
                    "baas": {"repo_dir": "", "current_activity": MODULE},
                    "activity": {"inject_activity_resources": True},
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    reports = r.json()["resource_reports"]
    assert reports and reports[0]["account"] == "acc_test"
    codes = [i["code"] for i in reports[0]["report"]["issues"]]
    assert codes == ["baas_unavailable"]


def test_upload_frame_and_delete(client, tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    root = _fake_baas_root(tmp_path / "baas")
    monkeypatch.setattr(BaasBridge, "_activity_resource_root", lambda self: str(root))
    client.put(
        "/api/config",
        json={
            "data_dir": str(tmp_path),
            "accounts": [
                {
                    "id": "acc_test",
                    "name": "测试账号",
                    "baas": {"repo_dir": "", "current_activity": MODULE},
                    "activity": {"inject_activity_resources": True},
                }
            ],
        },
    )
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 60, (720, 1280, 3), dtype=np.uint8)
    x1, y1, x2, y2 = BOXES["enter1"]
    cv2.rectangle(frame, (x1, y1), (x2, y2), (180, 150, 70), -1)
    cv2.circle(frame, ((x1 + x2) // 2, (y1 + y2) // 2), 13, (40, 40, 220), -1)
    ok, buf = cv2.imencode(".png", frame)
    assert ok

    r = client.post(
        f"/api/activity-resources/frame?account=acc_test&module={MODULE}&kind=main",
        content=buf.tobytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stored"]["saved"] == ["enter1"]
    assert body["report"]["ready"] is True
    assert body["stored"]["verification"]["enter1"]["score"] == 1.0
    stored = aa.patch_root(Path(tmp_path)) / "CN" / MODULE / "enter1.png"
    assert stored.is_file()

    # 删除还原
    r = client.delete(f"/api/activity-resources?account=acc_test&module={MODULE}")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert r.json()["report"]["ready"] is False


def test_upload_frame_rejects_bad_requests(client):
    r = client.post("/api/activity-resources/frame", content=b"")
    assert r.status_code == 400
    r = client.post("/api/activity-resources/frame?kind=weird", content=b"abc")
    assert r.status_code == 400
    assert "未知" in r.json()["detail"]


def test_saved_config_reports_resource_state(client, tmp_path, monkeypatch):
    """保存配置时即给出资源自检结果（不把问题留到运行时）"""
    root = _fake_baas_root(tmp_path / "baas")
    monkeypatch.setattr(BaasBridge, "_activity_resource_root", lambda self: str(root))
    r = client.put(
        "/api/config",
        json={
            "data_dir": str(tmp_path),
            "accounts": [
                {
                    "id": "acc_test",
                    "name": "测试账号",
                    "baas": {"repo_dir": "", "current_activity": MODULE},
                    "activity": {"inject_activity_resources": True},
                }
            ],
        },
    )
    assert r.status_code == 200
    report = r.json()["resource_reports"][0]["report"]
    assert report["module"] == MODULE
    assert report["ready"] is False
    assert "上传" in report["hint"]
    # 轮播图坐标框可从日服沿用（上游同款做法）
    assert report["boxes"]["enter1"] == list(BOXES["enter1"])


def test_config_without_activity_skips_report(client, tmp_path):
    r = client.put(
        "/api/config",
        json={"data_dir": str(tmp_path), "accounts": [{"id": "a1", "name": "n"}]},
    )
    assert r.status_code == 200
    assert r.json()["resource_reports"] == []


def test_saved_config_roundtrip_keeps_new_fields(client, tmp_path, _isolated_config_path):
    r = client.put(
        "/api/config",
        json={
            "data_dir": str(tmp_path),
            "accounts": [
                {
                    "id": "a1",
                    "name": "n",
                    "activity": {"inject_activity_resources": True, "push_before_sweep": False},
                }
            ],
        },
    )
    assert r.status_code == 200
    cfg = client.get("/api/config").json()
    activity = cfg["accounts"][0]["activity"]
    assert activity["inject_activity_resources"] is True
    assert activity["push_before_sweep"] is False
    # 旧配置（不含新字段）依然可用
    raw = json.loads(Path(_isolated_config_path).read_text(encoding="utf-8"))
    assert raw["accounts"][0]["activity"]["inject_activity_resources"] is True
