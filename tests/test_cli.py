"""CLI 入口回归测试"""
import logging
import sys
from pathlib import Path


def test_cli_import_without_data_dir(tmp_path, monkeypatch):
    """data/ 目录不存在时 cli 模块必须能正常 import（曾因 logging.FileHandler 炸掉）"""
    project_root = Path(__file__).resolve().parent.parent
    # 确保项目根下没有 data 目录，并阻止 cli 创建（模拟干净 clone）
    data_dir = project_root / "data"
    existed = data_dir.exists()
    if existed:
        # 备份后移除
        backup = tmp_path / "data_backup"
        data_dir.rename(backup)

    import importlib

    try:
        # 强制重新导入 cli（清掉已加载的模块）
        for name in list(sys.modules):
            if name.startswith("baas_plus.cli"):
                del sys.modules[name]
        cli = importlib.import_module("baas_plus.cli")
        assert cli is not None
        # 导入后 data/ 目录应被自动创建
        assert data_dir.exists()
    finally:
        if existed:
            # 恢复原 data 目录
            backup.rename(data_dir)
        elif data_dir.exists():
            # 测试创建的，删掉
            for f in data_dir.iterdir():
                f.unlink()
            data_dir.rmdir()


def test_cli_main_help(capsys):
    from baas_plus.cli import main

    try:
        main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "webui" in out
    assert "test-email" in out
