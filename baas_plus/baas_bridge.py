"""BAAS 集成层

职责：
- 启动模拟器并通过 Mumu/模拟器 API 获取 ADB 端口（复用 BAAS emulator_manager）
- 初始化 Baas_thread、执行任务（solve）、读取体力（get_ap）
- 运行时修改 BAAS 配置（扫荡列表 / 活动模块 / 扫荡次数）

BAAS 是可选依赖（官方 cli.example.py 同款用法）：
BAAS-Plus 作为库安装进 BAAS 的运行环境，从 BAAS 源码根目录运行（python -m baas_plus.cli ...），
此时 core 可直接 import、config/ 相对路径可用；也可配置 baas.repo_dir 指定 BAAS 源码目录。
本模块所有函数在 BAAS 未安装时抛出带指引的 RuntimeError。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any

from .config import AppConfig, SweepConfig

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.Baas_thread import Baas_thread

BAAS_IMPORT_ERROR = (
    "BAAS (blue_archive_auto_script) 未安装或无法导入。"
    "请确认：1) 在 BAAS 源码根目录运行（python -m baas_plus.cli ...）；"
    "2) 配置 baas.repo_dir 指向 BAAS 源码目录（如 D:\\BAAS）。"
)


def import_baas(repo_dir: str = "") -> Any:
    """惰性导入 BAAS 模块

    优先将 repo_dir（若配置）加入 sys.path 并切换 cwd 到 BAAS 源码根目录
    （BAAS 大量使用相对路径读 config/，必须从 BAAS 根运行）；未配置时假定
    BAAS-Plus 已安装进 BAAS 环境（cwd 即 BAAS 根）。
    """
    if repo_dir:
        if repo_dir not in sys.path:
            sys.path.insert(0, repo_dir)
        if os.path.abspath(os.getcwd()) != os.path.abspath(repo_dir):
            os.chdir(repo_dir)
    try:
        from core.Baas_thread import Baas_thread
        from core.config.config_set import ConfigSet
        from main import Main
    except ImportError as exc:
        # 区分两类原因：BAAS 本身不在路径 vs 依赖缺失
        if "No module named" in str(exc):
            missing = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
            raise RuntimeError(
                f"无法导入 BAAS 模块（缺少 {missing}）。\n"
                f"当前 repo_dir={repo_dir or '(空，假定已安装进 BAAS 环境)'}，"
                f"sys.path 前两项: {sys.path[:2]}\n"
                f"请确认：1) BAAS-Plus 已安装进 BAAS 的运行环境，且从 BAAS 源码根目录"
                f"运行（python -m baas_plus.cli webui）；2) 或配置 baas.repo_dir 指向 BAAS "
                f"源码目录（如 D:\\BAAS）。原始错误: {exc}"
            ) from exc
        raise RuntimeError(
            f"BAAS 模块导入失败（可能是依赖缺失或不兼容，如 numpy/opencv 与当前 Python 版本不匹配）。"
            f"原始错误: {exc}\n请确认 BAAS 的依赖已全部安装（pip install -r requirements.txt），"
            f"且 Python 版本受支持（BAAS 1.4.3 需 Python ≤3.12）。"
        ) from exc
    # 导入成功：修复 static.json 字段与代码不匹配的问题（幂等，仅字段不一致时修改）
    repair_static_config()
    return Baas_thread, ConfigSet, Main


def repair_static_config() -> None:
    """修复 BAAS config/static.json 与当前代码 StaticConfig 字段不匹配的问题

    上游 release 包的 static.json 可能与仓库源码字段不一致（例如 v1.4.3 的
    steam_app_process_name vs 源码的 PC_app_process_name），导致 ConfigSet 初始化
    抛 TypeError。以源码内置 STATIC_DEFAULT_CONFIG 为准对齐：删除多余键、补齐缺失键。
    调用前需保证 cwd 已是 BAAS 源码根目录（import_baas 已处理）。
    """
    try:
        from core.config.default_config import STATIC_DEFAULT_CONFIG
        from core.config.generated_static_config import StaticConfig
    except ImportError:
        return
    import dataclasses
    import json

    fields = {f.name for f in dataclasses.fields(StaticConfig)}
    default = json.loads(STATIC_DEFAULT_CONFIG)
    _align_json_file(os.path.join(os.getcwd(), "config", "static.json"), fields, default, "static.json")


def repair_user_config(config_dir: str) -> None:
    """修复 BAAS config/<config_dir>/config.json 与代码 Config 字段不匹配的问题

    同样处理上游 release 配置缺新字段的情况（如 v1.4.3 缺 ArenaStopFightWhenRank1 等），
    以源码 DEFAULT_CONFIG 补齐缺失字段（已有用户字段保留）。
    """
    try:
        from core.config.default_config import DEFAULT_CONFIG
        from core.config.generated_user_config import Config
    except ImportError:
        return
    import dataclasses
    import json

    fields = {f.name for f in dataclasses.fields(Config)}
    default = json.loads(DEFAULT_CONFIG)
    _align_json_file(os.path.join(os.getcwd(), "config", config_dir, "config.json"), fields, default, f"config/{config_dir}/config.json")


def _align_json_file(path: str, fields: set[str], default: dict, label: str) -> None:
    """对齐 JSON 配置文件字段：缺失补齐（用默认值）、多余删除（幂等）"""
    import json

    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        logger.info("已生成 %s（默认模板）", label)
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    extra = set(data.keys()) - fields
    missing = fields - set(data.keys())
    if not extra and not missing:
        return
    for key in extra:
        data.pop(key)
    for key in missing:
        data[key] = default.get(key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已修复 %s: 移除 %s, 补齐 %s", label, sorted(extra), sorted(missing))


class BaasBridge:
    """BAAS 操作封装（模拟器生命周期 + 任务执行 + 配置改写）"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.baas_thread: Baas_thread | None = None
        self._main = None
        self._started = False

    # ---- 模拟器 ----

    def start_simulator(self, wait_timeout: int = 180) -> str:
        """启动模拟器并等待其完全就绪，返回 ADB 地址（如 127.0.0.1:16384）

        BAAS 的 start_simulator_classic 只是发出启动指令（异步），不等待模拟器
        进程/ADB 就绪；这里通过 return_status 查询启动状态并轮询 ADB 连接，
        确保后续初始化 BAAS / 启动游戏时模拟器已可用。
        """
        import time

        import_baas(self.config.baas.repo_dir)
        # BAAS 1.4.x 中 emulator_manager 是 core/device/emulator_manager 包
        from core.device.emulator_manager.start_simulator import start_simulator_classic

        sim_type = self.config.simulator.type
        instance = self.config.simulator.instance
        logger.info("启动模拟器: %s 实例 %s", sim_type, instance)

        result = start_simulator_classic(sim_type, instance, return_status=True)
        if isinstance(result, (list, tuple)):
            status, adb_address = result[0], result[1]
            # 蓝叠在 return_status 下返回端口可能为 None（首次启动时端口未出现），重取一次
            if not adb_address:
                adb_address = start_simulator_classic(sim_type, instance)
        else:
            status, adb_address = None, result
        if not adb_address:
            raise RuntimeError(f"模拟器启动失败或未返回 ADB 地址: {sim_type} #{instance}")
        logger.info("模拟器启动指令已发出，ADB 地址: %s（启动状态: %s），等待就绪...", adb_address, status)

        self._wait_simulator_ready(adb_address, wait_timeout)
        self._started = True
        return adb_address

    def _wait_simulator_ready(self, adb_address: str, timeout: int = 180) -> None:
        """轮询等待模拟器 ADB 端口可用（模拟器冷启动可能耗时 1-3 分钟）"""
        import time

        try:
            from adbutils import adb
        except ImportError:
            logger.warning("adbutils 不可用，跳过模拟器就绪等待（超时 %ss）", timeout)
            time.sleep(10)
            return
        deadline = time.time() + timeout
        waited = 0
        while time.time() < deadline:
            try:
                adb.connect(adb_address)
                device = adb.device(adb_address)
                device.shell("echo ok", timeout=5)
                logger.info("模拟器 ADB 就绪（等待 %ss）", waited)
                return
            except Exception:
                time.sleep(3)
                waited += 3
        raise RuntimeError(f"等待模拟器就绪超时（{timeout}s）：{adb_address}，请确认模拟器已安装且可启动")

    # ---- BAAS 线程 ----

    def create_baas(self, adb_address: str | None = None) -> Baas_thread:
        """初始化 Baas_thread（OCR + 配置加载；首次较慢）"""
        Baas_thread, ConfigSet, Main = import_baas(self.config.baas.repo_dir)
        repair_user_config(self.config.baas.config_dir)

        # 无 GUI 模式初始化 OCR（对齐 BAAS 1.4.x OCR 语言：en-us 数字/英文 + zh-cn 中文）
        self._main = Main(ocr_needed=["en-us", "zh-cn"])

        config_set = ConfigSet(config_dir=self.config.baas.config_dir)
        baas = Baas_thread(config_set, None, None, None)
        # 必须先 set_ocr 再 init_all_data：set_ocr 会设置 ocr_img_pass_method
        # （本地 OCR=0 共享内存/远程=1）与 shared_memory_name，init_all_data →
        # init_device → check_resolution 依赖它；直接赋值 baas.ocr 会绕过该逻辑，
        # 导致 OCR 调用时报 Invalid pass_method None
        baas.set_ocr(self._main.ocr)
        baas.init_all_data()
        if adb_address:
            baas.set_adb_address(adb_address) if hasattr(baas, "set_adb_address") else None
        self.baas_thread = baas
        self.apply_game_package()
        return baas

    def solve(self, task: str) -> Any:
        """执行单个 BAAS 任务"""
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化，请先调用 create_baas()")
        logger.info("执行 BAAS 任务: %s", task)
        result = self.baas_thread.solve(task)
        self._last_next_time = getattr(self.baas_thread, "next_time", 0) or 0
        return result

    @property
    def last_next_time(self) -> int:
        """最近一次任务的冷却秒数（BAAS 任务可通过 next_time 请求延迟后再次执行）"""
        return getattr(self, "_last_next_time", 0)

    def get_ap(self) -> int:
        """读取当前体力（-1 表示读取失败）"""
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        return self.baas_thread.get_ap(True)

    # ---- 配置读取 ----

    def get_baas_sweep_config(self) -> dict[str, str]:
        """读取 BAAS 配置中的扫荡列表（mainlinePriority / hardPriority，格式 "区域-关卡-次数"）"""
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        config_set = self.baas_thread.config_set
        return {
            "mainlinePriority": str(config_set.get("mainlinePriority", "") or ""),
            "hardPriority": str(config_set.get("hardPriority", "") or ""),
        }

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
        repair_user_config(self.config.baas.config_dir)
        config_set = ConfigSet(config_dir=self.config.baas.config_dir)
        baas = Baas_thread(config_set, None, None, None)
        version = getattr(baas, "version", None) or getattr(baas, "__version__", None)
        self.apply_game_package()
        logger.info("BAAS 检查通过: config_dir=%s server=%s version=%s", self.config.baas.config_dir, self.config.baas.server, version)
        return {
            "import": "ok",
            "config_dir": self.config.baas.config_dir,
            "server": self.config.baas.server,
            "version": version,
        }

    # ---- BA 游戏包名 ----

    def apply_game_package(self, package_name: str | None = None) -> None:
        """将 BA 游戏包名写入 BAAS 共享 static_config（按当前服务器覆盖），供 restart/包检测使用

        覆盖时机：check_baas / create_baas 之后调用，后续所有 ConfigSet 实例共享生效。
        """
        import_baas(self.config.baas.repo_dir)
        _, ConfigSet, _ = import_baas(self.config.baas.repo_dir)

        pkg = package_name or self.config.baas.game_package_name
        if not pkg:
            return
        static = getattr(ConfigSet, "static_config", None)
        if static is None or not hasattr(static, "package_name"):
            logger.warning("BAAS static_config 未初始化或无 package_name，跳过包名设置")
            return
        # 先初始化一次 ConfigSet 拿 server（如 "官服"/"国际服"/"日服"）
        config_set = ConfigSet(config_dir=self.config.baas.config_dir)
        server = str(config_set.get("server") or "")
        if not server or server not in static.package_name:
            logger.warning("BAAS server=%r 不在 package_name 映射中，跳过包名设置", server)
            return
        static.package_name[server] = pkg
        # Baas_thread.package_name 在 init_all_data 时已从 static_config 拷贝，
        # 这里同步更新，确保 solve('restart') / launch_game 用新包名
        if self.baas_thread is not None:
            self.baas_thread.package_name = pkg
        logger.info("已设置 BA 包名: server=%s package=%s", server, pkg)

    def launch_game(self) -> None:
        """用配置的 BA 包名显式启动游戏并等待进入主界面（BAAS-Plus 负责打开 BA）

        替代裸 solve('restart')：restart 的包检测逻辑在 BA 已在前台时不会
        to_main_page；这里直接 app_start(配置包名) 并复用 BAAS 的 to_main_page
        处理启动画面/弹窗。
        """
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化，请先调用 create_baas()")
        baas = self.baas_thread
        self.apply_game_package()
        if getattr(baas, "u2", None) is None:
            raise RuntimeError("设备连接未就绪（u2 未初始化），无法启动游戏")
        pkg = self.config.baas.game_package_name
        activity = None
        server = getattr(baas, "server", None)
        if server and server != "CN" and getattr(baas, "activity_name", None):
            activity = baas.activity_name
        baas.u2.app_start(pkg, activity)
        logger.info("BAAS-Plus 启动游戏: package=%s activity=%s server=%s", pkg, activity, server)
        baas.to_main_page()

    def sync_sweep_from_baas(self) -> dict[str, Any]:
        """从 BAAS 配置读取扫荡列表并同步到 BAAS-Plus 配置（普通/困难图为空时填充）

        供 WebUI 保存「模拟器&BAAS」设置后调用；返回同步结果供前端提示。
        """
        if not self.config.baas.repo_dir:
            return {"ok": False, "reason": "baas.repo_dir 为空，无法读取 BAAS 配置"}
        self.check_baas()  # 初始化 ConfigSet/Baas_thread（含配置自愈）
        baas_cfg = self.get_baas_sweep_config()
        normal = [s.strip() for s in str(baas_cfg.get("mainlinePriority", "")).split(",") if s.strip()]
        hard = [s.strip() for s in str(baas_cfg.get("hardPriority", "")).split(",") if s.strip()]
        changed = False
        if not self.config.sweep.normal_tasks and normal:
            self.config.sweep.normal_tasks = normal
            changed = True
        if not self.config.sweep.hard_tasks and hard:
            self.config.sweep.hard_tasks = hard
            changed = True
        return {
            "ok": True,
            "normal_tasks": normal,
            "hard_tasks": hard,
            "applied": changed,
        }

    def stop(self) -> None:
        """停止 BAAS 线程并关闭模拟器（执行完成后调用）"""
        if self.baas_thread is not None:
            try:
                self.baas_thread.stop_thread() if hasattr(self.baas_thread, "stop_thread") else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止 BAAS 线程异常: %s", exc)
        self.baas_thread = None
        self._stop_simulator()

    def _stop_simulator(self) -> None:
        """关闭模拟器（对齐启动时的类型/实例；未启动/已关闭时幂等）"""
        try:
            import_baas(self.config.baas.repo_dir)
            from core.device.emulator_manager.stop_simulator import stop_simulator_classic

            logger.info("关闭模拟器: %s 实例 %s", self.config.simulator.type, self.config.simulator.instance)
            stop_simulator_classic(self.config.simulator.type, self.config.simulator.instance)
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭模拟器异常（可忽略）: %s", exc)


def compute_sweep_times(ap: int, base_cost: int, max_times: int) -> int:
    """按剩余体力计算扫荡次数（向上取整消耗，下限 0 上限 max_times）"""
    if ap <= 0 or base_cost <= 0:
        return 0
    return min(max_times, ap // base_cost)
