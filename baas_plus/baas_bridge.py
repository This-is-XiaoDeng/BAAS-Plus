"""BAAS 集成层

职责：
- 启动模拟器并通过 Mumu/模拟器 API 获取 ADB 端口（复用 BAAS emulator_manager）
- 初始化 Baas_thread、执行任务（solve）、读取体力（get_ap）
- 运行时修改 BAAS 配置（扫荡列表 / 活动模块 / 扫荡次数）

BAAS 是可选依赖（poetry extra: baas），仅在 Windows 部署环境安装；
本模块所有函数在 BAAS 未安装时抛出带指引的 RuntimeError。
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

from .config import AppConfig, SweepConfig

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.Baas_thread import Baas_thread

BAAS_IMPORT_ERROR = (
    "BAAS (blue_archive_auto_script) 未安装或无法导入。"
    "请确认：1) 已在 Windows 环境执行 poetry install -E baas（或 pip install 对应分支）；"
    "2) 配置 baas.repo_dir 指向 BAAS 源码目录。"
)


def import_baas(repo_dir: str = "") -> Any:
    """惰性导入 BAAS 模块；repo_dir 非空时优先加入 sys.path"""
    if repo_dir:
        if repo_dir not in sys.path:
            sys.path.insert(0, repo_dir)
    try:
        from core import emulator_manager  # noqa: F401
        from core.Baas_thread import Baas_thread
        from core.config.config_set import ConfigSet
        from main import Main

        return Baas_thread, ConfigSet, Main
    except ImportError as exc:
        # 区分两类原因：BAAS 本身不在路径 vs 依赖缺失
        if "No module named" in str(exc):
            missing = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
            raise RuntimeError(
                f"无法导入 BAAS 模块（缺少 {missing}）。\n"
                f"当前 repo_dir={repo_dir or '(空，用已安装包)'}，sys.path 前两项: {sys.path[:2]}\n"
                f"请确认：1) 在 poetry 虚拟环境内执行 poetry install -E baas（不要用系统 pip）；"
                f"2) 或配置 baas.repo_dir 指向 BAAS 源码目录（如 D:\\BAAS）。"
                f"原始错误: {exc}"
            ) from exc
        raise RuntimeError(
            f"BAAS 模块导入失败（可能是依赖缺失或不兼容，如 numpy/opencv 与当前 Python 版本不匹配）。"
            f"原始错误: {exc}\n请确认 BAAS 的依赖已全部安装（poetry install -E baas），"
            f"且 Python 版本受支持（BAAS 1.4.3 需 Python ≤3.12）。"
        ) from exc


class BaasBridge:
    """BAAS 操作封装（模拟器生命周期 + 任务执行 + 配置改写）"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.baas_thread: Baas_thread | None = None
        self._main = None
        self._started = False

    # ---- 模拟器 ----

    def start_simulator(self) -> str:
        """启动模拟器并返回 ADB 地址（如 127.0.0.1:16384）"""
        Baas_thread, _, _ = import_baas(self.config.baas.repo_dir)
        from core import emulator_manager

        sim_type = self.config.simulator.type
        instance = self.config.simulator.instance
        logger.info("启动模拟器: %s 实例 %s", sim_type, instance)

        adb_address = emulator_manager.start_simulator_classic(sim_type, instance)
        if not adb_address:
            raise RuntimeError(f"模拟器启动失败或未返回 ADB 地址: {sim_type} #{instance}")
        logger.info("模拟器已启动，ADB 地址: %s", adb_address)
        self._started = True
        return adb_address

    # ---- BAAS 线程 ----

    def create_baas(self, adb_address: str | None = None) -> Baas_thread:
        """初始化 Baas_thread（OCR + 配置加载；首次较慢）"""
        Baas_thread, ConfigSet, Main = import_baas(self.config.baas.repo_dir)

        # 无 GUI 模式初始化 OCR（参考官方 cli.example.py 用法）
        self._main = Main(ocr_needed=["NUM", "Global", self.config.baas.server])

        config_set = ConfigSet(config_dir=self.config.baas.config_dir)
        baas = Baas_thread(config_set, None, None, None)
        baas.init_all_data()
        baas.ocr = self._main.ocr
        if adb_address:
            baas.set_adb_address(adb_address) if hasattr(baas, "set_adb_address") else None
        self.baas_thread = baas
        return baas

    def solve(self, task: str) -> Any:
        """执行单个 BAAS 任务"""
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化，请先调用 create_baas()")
        logger.info("执行 BAAS 任务: %s", task)
        return self.baas_thread.solve(task)

    def get_ap(self) -> int:
        """读取当前体力（-1 表示读取失败）"""
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        return self.baas_thread.get_ap(True)

    # ---- 配置改写 ----

    def _config_set(self):
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        return self.baas_thread.config_set

    def set_current_activity(self, module_name: str) -> None:
        """设置 BAAS 当前活动模块（current_game_activity）"""
        self._config_set().set("current_game_activity", module_name)
        logger.info("已设置 BAAS 活动模块: %s", module_name)

    def set_sweep_tasks(self, normal_tasks: list[str], hard_tasks: list[str]) -> None:
        """设置普通/困难图扫荡列表（mainlinePriority / hardPriority，格式 region-mission-counts）

        设置后重新解析 unfinished 任务列表（BAAS 扫荡任务实际消费的数据）
        """
        config_set = self._config_set()
        config_set.set("mainlinePriority", normal_tasks)
        config_set.set("hardPriority", hard_tasks)
        baas = self.baas_thread
        if baas is not None:
            refresh = getattr(baas, "refresh_common_tasks", None)
            if refresh:
                refresh()
            refresh_hard = getattr(baas, "refresh_hard_tasks", None)
            if refresh_hard:
                refresh_hard()
        logger.info("已设置扫荡列表: normal=%s hard=%s", normal_tasks, hard_tasks)

    def set_activity_sweep(self, task_number: str, times: str) -> None:
        """设置活动扫荡：关卡号 + 次数（"-1" = 按 AP 自动计算）"""
        config_set = self._config_set()
        config_set.set("activity_sweep_task_number", task_number)
        config_set.set("activity_sweep_times", times)
        logger.info("已设置活动扫荡: 关卡=%s 次数=%s", task_number, times)

    # ---- 生命周期 ----

    def check_baas(self) -> dict[str, Any]:
        """轻量验证 BAAS：导入 + 配置加载 + 线程构造（不启动模拟器/OCR，供 WebUI 测试按钮）"""
        Baas_thread, ConfigSet, _ = import_baas(self.config.baas.repo_dir)
        config_set = ConfigSet(config_dir=self.config.baas.config_dir)
        baas = Baas_thread(config_set, None, None, None)
        version = getattr(baas, "version", None) or getattr(baas, "__version__", None)
        logger.info("BAAS 检查通过: config_dir=%s server=%s version=%s", self.config.baas.config_dir, self.config.baas.server, version)
        return {
            "import": "ok",
            "config_dir": self.config.baas.config_dir,
            "server": self.config.baas.server,
            "version": version,
        }

    def stop(self) -> None:
        if self.baas_thread is not None:
            try:
                self.baas_thread.stop_thread() if hasattr(self.baas_thread, "stop_thread") else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止 BAAS 线程异常: %s", exc)
        self.baas_thread = None


def compute_sweep_times(ap: int, base_cost: int, max_times: int) -> int:
    """按剩余体力计算扫荡次数（向上取整消耗，下限 0 上限 max_times）"""
    if ap <= 0 or base_cost <= 0:
        return 0
    return min(max_times, ap // base_cost)
