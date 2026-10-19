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
    # 活动设置为全局字段（真实字段而非代理）
    assert config.activity.current_activity == ""
    assert config.activity.banner_region == [1109, 133, 1280, 281]
    assert config.activity.push_story_on_new is True
    assert config.activity.inject_activity_resources is False


def test_activity_config_is_global_not_per_account():
    """活动设置挂在全局（AppConfig.activity），账号不再持有 activity"""
    config = AppConfig(
        activity={"current_activity": "SayBing"},
        accounts=[{"name": "主号"}, {"name": "小号"}],
    )
    assert config.activity.current_activity == "SayBing"
    assert not hasattr(config.accounts[0], "activity")
    # 旧版 baas 上的活动字段也已移除
    assert not hasattr(config.accounts[0].baas, "current_activity")
    assert not hasattr(config.accounts[0].baas, "banner_region")


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
    # 旧版账号级活动设置提升为全局 activity
    assert migrated["activity"] == {"push_story_on_new": False}


def test_migrate_legacy_activity_baas_fields_promoted():
    """旧版 baas.current_activity / banner_region 提升为全局 activity 字段"""
    data = {
        "accounts": [
            {
                "id": "acc_a",
                "baas": {"server": "cn", "current_activity": "SayBing"},
                "activity": {"push_mission_on_new": True, "server": "cn"},
            }
        ]
    }
    migrated = _migrate_legacy_config(data)
    assert migrated["activity"] == {
        "push_mission_on_new": True,
        "current_activity": "SayBing",
    }


def test_migrate_multi_account_activity_promoted_from_first_account():
    """多账号旧配置：全局活动设置取 accounts[0]（不再按账号配置）"""
    data = {
        "accounts": [
            {"id": "a1", "activity": {"push_story_on_new": False}, "baas": {"current_activity": "CodeBox"}},
            {"id": "a2", "activity": {"push_story_on_new": True}},
        ]
    }
    migrated = _migrate_legacy_config(data)
    assert migrated["activity"]["push_story_on_new"] is False
    assert migrated["activity"]["current_activity"] == "CodeBox"


def test_migrate_keeps_existing_global_activity():
    """已有全局 activity 时不被账号级旧字段覆盖（全局为准）"""
    data = {
        "activity": {"current_activity": "Kept"},
        "accounts": [{"id": "a1", "activity": {"current_activity": "Old"}, "baas": {}}],
    }
    migrated = _migrate_legacy_config(data)
    assert migrated["activity"] == {"current_activity": "Kept"}


def test_load_legacy_activity_config_roundtrip(tmp_path):
    """旧配置（账号级活动设置）加载后活动设置在全局，保存/加载往返一致"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "accounts": [
                    {"id": "acc_a", "baas": {"server": "jp"}, "activity": {"push_mission_on_new": True}}
                ]
            }
        ),
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.activity.push_mission_on_new is True
    assert config.accounts[0].baas.server == "jp"
    save_config(config, path)
    reloaded = load_config(str(path))
    assert reloaded.activity.push_mission_on_new is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["activity"]["push_mission_on_new"] is True


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


def test_special_task_sweep_defaults():
    """特别委托扫荡开关默认关闭、次数默认 0,max（向后兼容：旧配置无该字段）"""
    config = AppConfig()
    assert config.sweep.special_task_when_no_activity is False
    assert config.sweep.special_task_times == "0,max"


def test_special_task_sweep_roundtrip(tmp_path):
    config = AppConfig()
    config.sweep.special_task_when_no_activity = True
    config.sweep.special_task_times = "max,0"
    path = save_config(config, str(tmp_path / "config.json"))
    loaded = load_config(str(path))
    assert loaded.sweep.special_task_when_no_activity is True
    assert loaded.sweep.special_task_times == "max,0"


def test_run_times_default_and_roundtrip(tmp_path):
    """多次执行轮数：默认 1（兼容旧配置），保存/加载往返，非法值（<1）拒绝"""
    assert AppConfig().run_times == 1
    config = AppConfig(run_times=3)
    assert config.run_times == 3
    path = save_config(config, str(tmp_path / "config.json"))
    loaded = load_config(str(path))
    assert loaded.run_times == 3
    with pytest.raises(ValueError):
        AppConfig(run_times=0)  # ge=1


def test_legacy_config_run_times_defaults_to_one(tmp_path):
    """旧版 config.json 无 run_times 字段 → 默认 1（向后兼容）"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"baas": {"server": "cn"}, "data_dir": str(tmp_path)}),
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.run_times == 1


@pytest.mark.parametrize("times", ["0,max", "max,0", "3,5", "max,max", "0, 1"])
def test_special_task_times_valid(times):
    account = AccountConfig(sweep={"special_task_times": times})
    assert "," in account.sweep.special_task_times


@pytest.mark.parametrize(
    "times", ["", "max", "1,2,3", "-1,2", "a,b", "1.5,2", "max;0"]
)
def test_special_task_times_invalid_rejected(times):
    with pytest.raises(ValueError):
        AccountConfig(sweep={"special_task_times": times})


# ---- 游戏内切换账号（AccountSwitchConfig） ----


def _switch_accounts():
    """构造主号/小号（游戏账号序号 1/2，共用默认 mumu 实例 0）的 accounts 配置"""
    return [
        {"name": "主号", "game_account_index": 1, "baas": {"tasks": []}},
        {"name": "小号", "game_account_index": 2, "baas": {"tasks": []}},
    ]


def test_account_switch_defaults_off():
    """默认关闭：旧配置无需任何 account_switch 字段即可正常加载"""
    config = AppConfig()
    assert config.account_switch.enabled is False
    assert config.account_switch.switch_button == (60, 688)
    assert config.account_switch.confirm_button == (736, 415)
    assert config.account_switch.dropdown_button == (855, 315)
    assert config.account_switch.enter_game_button == (642, 430)
    assert config.account_switch.first_account_pos == (600, 420)
    assert config.account_switch.account_row_height == 100
    # 等登录界面出现默认 300s：实测「重开 → Yostar 登录框出现」约 106~114s，
    # 旧的 120s 只留 6~14s 余量（远端/低配模拟器必超时），故放大到 300s
    assert config.account_switch.app_start_timeout == 300.0
    # 盲等兜底默认 300s（等于 app_start_timeout）：正常路径靠 BAAS main_page
    # 特征正面判定自动登录，盲等只在短于 app_start_timeout 时才生效
    assert config.account_switch.blind_restart_after == 300.0
    # 登出后等待 Yostar 选择框默认 300s（五分钟），旧配置缺字段时向后兼容
    assert config.account_switch.dialog_timeout == 300.0
    # 账号默认未设置游戏账号序号（0），不开启切换模式时无影响
    assert config.accounts[0].game_account_index == 0


def test_migrate_legacy_switch_timeout_defaults():
    """旧版 WebUI 写进配置的 120s（当时的默认值）在加载时回落到当前默认 300s

    回归：用户升级到「默认 300s」的版本后仍报「等待登录界面超时（120s）」——
    旧版前端没有 app_start_timeout 控件，保存时按 120 回填进 config.json，
    而 pydantic 默认值只对**缺失**字段生效，光改代码默认值救不了已有配置。
    """
    data = {
        "accounts": [{"id": "acc_default", "name": "默认账号", "game_account_index": 1}],
        "account_switch": {
            "enabled": True,
            "app_start_timeout": 120.0,
            "blind_restart_after": 120.0,
            "dialog_timeout": 300.0,
        },
    }
    migrated = _migrate_legacy_config(data)
    assert "app_start_timeout" not in migrated["account_switch"]
    assert "blind_restart_after" not in migrated["account_switch"]
    assert migrated["account_switch"]["dialog_timeout"] == 300.0  # 其他字段不动
    assert data["account_switch"]["app_start_timeout"] == 120.0  # 原 dict 不被改写
    config = AppConfig.model_validate(migrated)
    assert config.account_switch.app_start_timeout == 300.0
    assert config.account_switch.blind_restart_after == 300.0


def test_migrate_keeps_custom_switch_timeouts():
    """用户自己调过的超时值（不等于旧默认值）不迁移"""
    data = {
        "accounts": [{"id": "acc_default", "name": "默认账号"}],
        "account_switch": {"app_start_timeout": 600.0, "blind_restart_after": 90.0},
    }
    assert _migrate_legacy_config(data) is data


def test_load_config_migrates_legacy_switch_timeout(tmp_path):
    """端到端：旧配置落盘 → load_config 拿到放大后的默认超时"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "accounts": [{"id": "acc_default", "name": "默认账号", "game_account_index": 1}],
                "account_switch": {"enabled": True, "app_start_timeout": 120},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.account_switch.app_start_timeout == 300.0


def test_account_switch_roundtrip(tmp_path):
    config = AppConfig(
        account_switch={"enabled": True, "confirm_button": [700, 400], "dialog_timeout": 90},
        accounts=_switch_accounts(),
    )
    path = save_config(config, str(tmp_path / "config.json"))
    loaded = load_config(str(path))
    assert loaded.account_switch.enabled is True
    assert loaded.account_switch.confirm_button == (700, 400)
    assert loaded.account_switch.dialog_timeout == 90.0
    assert [a.game_account_index for a in loaded.accounts] == [1, 2]


def test_account_switch_rejects_different_simulator_instances():
    """切换模式开启时：多账号必须共用同一模拟器实例（不能再多开）"""
    with pytest.raises(ValueError, match="同一个模拟器实例"):
        AppConfig(
            account_switch={"enabled": True},
            accounts=[
                {"name": "主号", "game_account_index": 1, "simulator": {"instance": 0}},
                {"name": "小号", "game_account_index": 2, "simulator": {"instance": 1}},
            ],
        )


def test_account_switch_rejects_missing_index():
    """切换模式开启时：每个账号必须设置游戏账号序号（>= 1）"""
    with pytest.raises(ValueError, match="游戏账号序号"):
        AppConfig(
            account_switch={"enabled": True},
            accounts=[
                {"name": "主号", "game_account_index": 1},
                {"name": "小号", "game_account_index": 0},
            ],
        )


def test_account_switch_rejects_duplicate_index():
    """切换模式开启时：游戏账号序号必须互不相同（否则两个账号点到同一个号）"""
    with pytest.raises(ValueError, match="互不相同"):
        AppConfig(
            account_switch={"enabled": True},
            accounts=[
                {"name": "主号", "game_account_index": 1},
                {"name": "小号", "game_account_index": 1},
            ],
        )


def test_account_switch_accepts_phone_tail_without_index():
    """切换模式开启时：设置手机尾号即可（列表顺序不固定，尾号匹配优先于序号）"""
    config = AppConfig(
        account_switch={"enabled": True},
        accounts=[
            {"name": "主号", "game_account_phone_tail": "3522", "baas": {"tasks": []}},
            {"name": "小号", "game_account_phone_tail": "9524", "baas": {"tasks": []}},
        ],
    )
    assert [a.game_account_phone_tail for a in config.accounts] == ["3522", "9524"]


def test_account_switch_rejects_missing_tail_and_index():
    """切换模式开启时：既没有尾号也没有序号的账号拒绝加载"""
    with pytest.raises(ValueError, match="手机尾号"):
        AppConfig(
            account_switch={"enabled": True},
            accounts=[
                {"name": "主号", "game_account_index": 1},
                {"name": "小号"},  # 尾号与序号都没设置
            ],
        )


def test_account_switch_rejects_duplicate_tail():
    """切换模式开启时：手机尾号必须互不相同（否则 OCR 会匹配到同一条目）"""
    with pytest.raises(ValueError, match="手机尾号必须互不相同"):
        AppConfig(
            account_switch={"enabled": True},
            accounts=[
                {"name": "主号", "game_account_phone_tail": "3522"},
                {"name": "小号", "game_account_phone_tail": "3522"},
            ],
        )


def test_account_rejects_non_digit_tail():
    """手机尾号必须是纯数字（OCR 匹配按数字串比对）"""
    with pytest.raises(ValueError, match="纯数字"):
        AppConfig(accounts=[{"name": "主号", "game_account_phone_tail": "3a22"}])


def test_index_zero_allowed_when_switch_disabled():
    """切换模式关闭时：序号未设置（0）/重复都不影响配置加载（旧配置兼容）"""
    config = AppConfig(
        accounts=[
            {"name": "主号", "simulator": {"instance": 0}},
            {"name": "小号", "simulator": {"instance": 1}},
        ]
    )
    assert [a.game_account_index for a in config.accounts] == [0, 0]
