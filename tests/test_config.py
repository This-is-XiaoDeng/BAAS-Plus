"""配置模型测试"""
import json
from pathlib import Path

import pytest

from baas_plus.config import (
    AccountConfig,
    AppConfig,
    BAAS_TASKS,
    _migrate_legacy_config,
    load_config,
    save_config,
)


def test_default_config():
    config = AppConfig()
    assert config.baas.server == "cn"
    assert config.simulator.type == "mumu"
    assert config.sweep.strategy == "auto"
    assert config.notify.email.smtp_host == "smtp.qq.com"
    # 兼容属性代理到默认账号
    assert config.baas is config.accounts[0].baas


def test_task_validation():
    with pytest.raises(ValueError):
        AppConfig(accounts=[{"baas": {"tasks": ["not_exist_task"]}}])


def test_save_and_load(tmp_path):
    config = AppConfig()
    config.baas.tasks = ["cafe_reward", "lesson"]
    config.sweep.normal_tasks = ["15-1-3"]
    path = save_config(config, str(tmp_path / "config.json"))
    loaded = load_config(str(path))
    assert loaded.baas.tasks == ["cafe_reward", "lesson"]
    assert loaded.sweep.normal_tasks == ["15-1-3"]


def test_load_missing_returns_default(tmp_path):
    config = load_config(str(tmp_path / "nope.json"))
    default = AppConfig()
    # 账号 id 是随机生成的，比较时排除（其余字段应完全一致）
    exclude = {"accounts": {"__all__": {"id"}}}
    assert config.model_dump(exclude=exclude) == default.model_dump(exclude=exclude)


def test_data_path_absolute(tmp_path):
    config = AppConfig(data_dir=str(tmp_path))
    assert config.data_path == tmp_path


def test_tasks_list_nonempty():
    assert "cafe_reward" in BAAS_TASKS
    assert "activity_sweep" in BAAS_TASKS


def test_game_package_name_default():
    """BA 游戏包名默认值 = 国服官服包名"""
    config = AppConfig()
    assert config.baas.game_package_name == "com.RoamingStar.BlueArchive"


def test_game_package_name_roundtrip(tmp_path):
    config = AppConfig()
    config.baas.game_package_name = "com.custom.bluearchive"
    path = save_config(config, str(tmp_path / "config.json"))
    loaded = load_config(str(path))
    assert loaded.baas.game_package_name == "com.custom.bluearchive"


# ---- 多账号结构 ----


def test_accounts_default_single():
    config = AppConfig()
    assert len(config.accounts) == 1
    assert config.accounts[0].name == "默认账号"
    assert config.accounts[0].enabled is True
    assert config.accounts[0].id.startswith("acc_")


def test_multiple_accounts_independent():
    config = AppConfig(
        accounts=[
            {"name": "主号", "simulator": {"instance": 0}},
            {"name": "小号", "simulator": {"instance": 1}, "baas": {"tasks": ["mail"]}},
        ]
    )
    assert len(config.accounts) == 2
    assert config.accounts[0].simulator.instance == 0
    assert config.accounts[1].simulator.instance == 1
    assert config.accounts[1].baas.tasks == ["mail"]
    assert config.accounts[0].baas.tasks != config.accounts[1].baas.tasks


def test_account_id_stable_across_rename(tmp_path):
    """改名不影响 id（执行记录/活动状态按 id 关联）"""
    config = AppConfig(accounts=[{"name": "A"}])
    acc_id = config.accounts[0].id
    config.accounts[0].name = "改名前"
    path = save_config(config, str(tmp_path / "config.json"))
    loaded = load_config(str(path))
    assert loaded.accounts[0].id == acc_id
    assert loaded.accounts[0].name == "改名前"


def test_at_least_one_account_required():
    with pytest.raises(ValueError):
        AppConfig(accounts=[])


def test_duplicate_account_id_rejected():
    with pytest.raises(ValueError):
        AppConfig(accounts=[{"id": "acc_x"}, {"id": "acc_x"}])


def test_account_notify_to_addrs_override():
    config = AppConfig(accounts=[{"name": "主号", "notify_to_addrs": ["a@qq.com"]}])
    assert config.accounts[0].notify_to_addrs == ["a@qq.com"]
    assert AppConfig().accounts[0].notify_to_addrs is None  # 默认用全局


# ---- 旧配置迁移 ----


def test_migrate_legacy_config_plain():
    """非旧配置（无账号字段）原样返回"""
    data = {"webui": {"port": 18080}, "notify": {"enabled": True}}
    assert _migrate_legacy_config(data) is data


def test_migrate_legacy_config_new_structure_untouched():
    data = {"accounts": [{"name": "X"}], "webui": {}}
    assert _migrate_legacy_config(data) is data


def test_migrate_legacy_config_wraps_account_fields():
    """旧版顶层账号字段 → accounts[0]（id=acc_default），notify/webui 保持全局"""
    data = {
        "webui": {"port": 18080},
        "notify": {"enabled": True, "email": {"to_addrs": ["x@qq.com"]}},
        "data_dir": "data",
        "simulator": {"type": "leidian", "instance": 2},
        "baas": {"server": "jp", "tasks": ["mail"]},
        "activity": {"push_story_on_new": False},
        "sweep": {"strategy": "fixed", "fixed_times": 3},
    }
    migrated = _migrate_legacy_config(data)
    assert "simulator" not in migrated and "baas" not in migrated
    assert migrated["notify"]["email"]["to_addrs"] == ["x@qq.com"]  # 全局保留
    account = migrated["accounts"][0]
    assert account["id"] == "acc_default"
    assert account["name"] == "默认账号"
    assert account["simulator"]["instance"] == 2
    assert account["baas"]["server"] == "jp"
    assert account["sweep"]["strategy"] == "fixed"


def test_load_legacy_config_file(tmp_path):
    """旧版 config.json 直接可加载，行为与升级前一致"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "simulator": {"type": "mumu", "instance": 3},
                "baas": {"server": "in", "tasks": ["cafe_reward", "mail"]},
                "sweep": {"normal_tasks": ["15-1-3"]},
                "notify": {"enabled": True},
                "data_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert len(config.accounts) == 1
    acc = config.accounts[0]
    assert acc.id == "acc_default"
    assert acc.simulator.instance == 3
    assert acc.baas.server == "in"
    assert acc.sweep.normal_tasks == ["15-1-3"]
    assert config.notify.enabled is True
    # 兼容属性同样可访问
    assert config.baas.server == "in"
    assert config.simulator.instance == 3


def test_legacy_config_save_writes_new_structure(tmp_path):
    """旧配置加载后再保存 → 落盘新结构（accounts 包裹）"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"baas": {"server": "cn"}, "simulator": {"instance": 1}, "data_dir": str(tmp_path)}),
        encoding="utf-8",
    )
    config = load_config(str(path))
    save_config(config, str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "accounts" in saved
    assert saved["accounts"][0]["id"] == "acc_default"
    assert "baas" not in saved  # 顶层不再有账号字段
