"""配置模型测试"""
import json
from pathlib import Path

import pytest

from baas_plus.config import AppConfig, BAAS_TASKS, load_config, save_config


def test_default_config():
    config = AppConfig()
    assert config.baas.server == "cn"
    assert config.simulator.type == "mumu"
    assert config.sweep.strategy == "auto"
    assert config.notify.email.smtp_host == "smtp.qq.com"


def test_task_validation():
    with pytest.raises(ValueError):
        AppConfig(**{"baas": {"tasks": ["not_exist_task"]}})


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
    assert config == AppConfig()


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
