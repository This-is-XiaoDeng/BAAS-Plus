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
import json
import os
import re
import sys
import time
from typing import TYPE_CHECKING, Any

from .config import AppConfig, SweepConfig

logger = logging.getLogger(__name__)

# BAAS 扫荡列表项格式：region-mission-counts（counts 可为 max 或数字）
SWEEP_ITEM_RE = re.compile(r"\d+-\d+-(?:max|\d+)")


def _parse_sweep_list(value: object) -> list[str]:
    """解析 BAAS 扫荡列表（mainlinePriority/hardPriority）

    兼容三种形态：
    1. 正常 str："3-3-3,9-3-3"
    2. list（BAAS-Plus 旧版误写入 JSON 数组）
    3. 历史嵌套转义污染（"['[\\'[\\\\\\'...3-3-3"，多次 str(list)+split 累积）

    统一用正则提取所有 region-mission-counts 干净项，天然过滤引号/括号垃圾。
    """
    text = value if isinstance(value, str) else str(value)
    return SWEEP_ITEM_RE.findall(text)

if TYPE_CHECKING:
    from core.Baas_thread import Baas_thread

BAAS_IMPORT_ERROR = (
    "BAAS (blue_archive_auto_script) 未安装或无法导入。"
    "请确认：1) 在 BAAS 源码根目录运行（python -m baas_plus.cli ...）；"
    "2) 配置 baas.repo_dir 指向 BAAS 源码目录（如 D:\\BAAS）。"
)


MISSION_ARMOR_MAP = {
    # 敌人防御类型（白名单，子串匹配）→ 克制属性（对应 BAAS preset_team_attribute 键）
    # 游戏内共 6 种装甲，克制关系按游戏内「克制信息」表（注意与 BA 标准不同）：
    #   一般装甲: 全属性普通(100%)，主人指定用爆发队
    #   轻装甲:   爆发 200%
    #   重装甲:   贯穿 200%（非神秘！）
    #   复合装甲: 分解 200%（新属性，BAAS 预设体系无对应 → 不覆盖）
    #   特殊装甲: 神秘 200%（非贯穿！）
    #   弹力装甲: 振動 200%
    "一般装甲": "burst",
    "轻装甲": "burst",
    "重装甲": "pierce",
    "特殊装甲": "mystic",
    "弹力装甲": "shock",
    "弹性装甲": "shock",  # 兼容 OCR 变体
}


def import_baas(repo_dir: str = "") -> Any:
    """惰性导入 BAAS 模块

    优先将 repo_dir（若配置）加入 sys.path 并切换 cwd 到 BAAS 源码根目录
    （BAAS 大量使用相对路径读 config/，必须从 BAAS 根运行）；未配置时假定
    BAAS-Plus 已安装进 BAAS 环境（cwd 即 BAAS 根）。
    """
    # 防御：BAAS 内部大量 requests.get() 不带 timeout，OCR 服务器/设备接口
    # 无响应时会无限挂起。设置 socket 默认超时兜底（httpx 等显式超时不受影响）
    import socket

    if socket.getdefaulttimeout() is None:
        socket.setdefaulttimeout(20)
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

    def restart_simulator(self) -> bool:
        """完整重启模拟器并恢复到游戏主界面（模拟器/游戏失联时的自动恢复）

        顺序：停 BAAS 线程 → 关闭模拟器 → 重新启动并等待 ADB 就绪 → 重新初始化
        BAAS（复用已启动的 OCR 服务器）→ 重新启动游戏进入主界面。任一步失败
        返回 False，由调用方决定跳过扫荡继续执行，而不是崩溃。

        注意：重启前通过 BAAS config.json 持久化的设置（扫荡列表 / 活动模块等）
        会在重新加载 ConfigSet 后保留。
        """
        logger.warning("检测到模拟器/游戏失联，尝试重启模拟器恢复...")
        try:
            self.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止 BAAS/关闭模拟器异常（继续重启流程）: %s", exc)
        try:
            adb = self.start_simulator()
            self.create_baas(adb)
        except Exception as exc:  # noqa: BLE001
            logger.error("重启模拟器失败: %s", exc)
            return False
        try:
            self.launch_game()
        except Exception as exc:  # noqa: BLE001
            logger.error("模拟器重启后启动游戏失败: %s", exc)
            return False
        logger.info("模拟器重启完成，游戏已回到主界面")
        return True

    # ---- BAAS 线程 ----

    def create_baas(self, adb_address: str | None = None) -> Baas_thread:
        """初始化 Baas_thread（OCR + 配置加载；首次较慢）"""
        Baas_thread, ConfigSet, Main = import_baas(self.config.baas.repo_dir)
        repair_user_config(self.config.baas.config_dir)

        # 无 GUI 模式初始化 OCR（对齐 BAAS 1.4.x OCR 语言：en-us 数字/英文 + zh-cn 中文）。
        # OCR 服务器是独立进程，重启模拟器（restart_simulator）会再次走到这里，
        # 复用已启动的 Main 避免重复拉起 OCR 服务器进程（BAAS 客户端每次会找空闲端口，
        # 重复创建虽不冲突但会泄漏进程）
        if self._main is None:
            logger.info("初始化 BAAS Main（启动 OCR 服务器，首次可能较慢）...")
            self._main = Main(ocr_needed=["en-us", "zh-cn"])
            logger.info("BAAS Main 初始化完成")
        # BAAS 的 Main 构造可能重置 root logger 的 handlers（BAAS 自带日志配置），
        # 重新确保 baas_plus 独立命名空间的 FileHandler 仍在，避免后续日志丢失
        from .log_setup import setup_logging

        setup_logging()

        config_set = ConfigSet(config_dir=self.config.baas.config_dir)
        logger.info("ConfigSet 加载完成: config_dir=%s", self.config.baas.config_dir)
        baas = Baas_thread(config_set, None, None, None)
        # 必须先 set_ocr 再 init_all_data：set_ocr 会设置 ocr_img_pass_method
        # （本地 OCR=0 共享内存/远程=1）与 shared_memory_name，init_all_data →
        # init_device → check_resolution 依赖它；直接赋值 baas.ocr 会绕过该逻辑，
        # 导致 OCR 调用时报 Invalid pass_method None
        baas.set_ocr(self._main.ocr)
        logger.info("OCR 已设置（pass_method=%s）", baas.ocr_img_pass_method)
        logger.info("开始 Baas_thread.init_all_data（设备连接/分辨率/资源加载）...")
        baas.init_all_data()
        logger.info("Baas_thread.init_all_data 完成")
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
        baas = self.baas_thread
        # 对齐 BAAS 官方调度器（Baas_thread.thread_starter）语义：current_task 执行前
        # 把 next_time 重置为 0，任务内按需再设置。直接调 solve() 会绕过该重置，
        # 导致 next_time 残留上一场遗留的冷却值：arena 最后一票（票数==1）打完只
        # 领奖励、不归零 next_time，引擎会误判「还有票」而派发不存在的第 6 场
        # （白等一个冷却周期）。重置后 last_next_time<=0 即表示票已用完。
        if hasattr(baas, "next_time"):
            baas.next_time = 0
        try:
            result = baas.solve(task)
        finally:
            self._last_next_time = getattr(baas, "next_time", 0) or 0
        return result

    @property
    def last_next_time(self) -> int:
        """最近一次任务的冷却秒数（BAAS 任务可通过 next_time 请求延迟后再次执行）"""
        return getattr(self, "_last_next_time", 0)

    def get_ap(self) -> int:
        """读取当前体力（-1 表示读取失败）

        BAAS 的 get_ap() 用缓存帧 latest_img_array 做 OCR，**不会主动刷新截图**；
        任务结束后若最后一次截图失败（帧为 None），BAAS OCR 会抛
        TypeError: 'NoneType' object is not subscriptable，把整个执行拖垮
        （现象：所有任务完成后 run_sweep 阶段崩溃）。
        这里沿用 match_banner_activity / ocr_banner 的模式：先
        update_screenshot_array() 刷新截图帧，无有效帧或 OCR 异常时重试，
        最终仍失败返回 -1，让引擎跳过扫荡而不是抛异常终止。
        """
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        baas = self.baas_thread
        for attempt in range(3):
            update = getattr(baas, "update_screenshot_array", None)
            if callable(update):
                try:
                    update()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取体力前刷新截图失败（第 %d 次）: %s", attempt + 1, exc)
                    if attempt < 2:
                        time.sleep(1)
                    continue
            img = getattr(baas, "latest_img_array", None)
            if img is None:
                logger.warning("读取体力失败：截图帧为空（第 %d 次），重试...", attempt + 1)
                if attempt < 2:
                    time.sleep(1)
                continue
            try:
                return baas.get_ap(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取体力 OCR 失败（第 %d 次）: %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(1)
        logger.warning("读取体力失败：多次刷新截图均无有效帧，跳过扫荡")
        return -1

    # ---- 配置读取 ----

    def _baas_config_path(self) -> str | None:
        """BAAS config.json 绝对路径（repo_dir/config/<config_dir>/config.json）"""
        repo, cfg_dir = self.config.baas.repo_dir, self.config.baas.config_dir
        if not repo or not cfg_dir:
            return None
        path = os.path.join(repo, "config", cfg_dir, "config.json")
        return path if os.path.exists(path) else None

    def _read_baas_config_file(self) -> dict | None:
        """纯文件读取 BAAS config.json（不依赖 Baas_thread，供 WebUI/保存配置场景）"""
        path = self._baas_config_path()
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 BAAS config.json 失败 %s: %s", path, exc)
            return None

    def _write_baas_config_file(self, data: dict) -> bool:
        """纯文件写入 BAAS config.json（临时文件 + 原子替换）"""
        path = self._baas_config_path()
        if path is None:
            return False
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 BAAS config.json 失败 %s: %s", path, exc)
            return False

    def get_baas_sweep_config(self) -> dict[str, list[str]]:
        """读取 BAAS 配置中的扫荡列表（mainlinePriority / hardPriority）

        返回 region-mission-counts 干净列表（自动过滤历史嵌套转义污染）。
        优先纯文件读取（无需 Baas_thread）；文件不可用时回退 ConfigSet。
        """
        data = self._read_baas_config_file()
        if data is not None:
            return {
                "mainlinePriority": _parse_sweep_list(data.get("mainlinePriority", "")),
                "hardPriority": _parse_sweep_list(data.get("hardPriority", "")),
            }
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化且 config.json 不可读")
        config_set = self.baas_thread.config_set
        return {
            "mainlinePriority": _parse_sweep_list(config_set.get("mainlinePriority", "")),
            "hardPriority": _parse_sweep_list(config_set.get("hardPriority", "")),
        }

    # ---- 配置改写 ----

    def _config_set(self):
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        return self.baas_thread.config_set

    # ---- BAAS 更新检查 ----

    def get_local_baas_version(self) -> str | None:
        """读取本地 BAAS 版本（repo_dir/pyproject.toml 的 version 字段）"""
        repo = self.config.baas.repo_dir
        if not repo:
            return None
        path = os.path.join(repo, "pyproject.toml")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            # 正则匹配首个 version 行（[project] 表），零依赖兼容 Py3.9+
            m = re.search(r"^version\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
            return m.group(1) if m else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 BAAS 版本失败: %s", exc)
            return None

    def check_baas_update(self, timeout: float = 8.0) -> dict | None:
        """检查 BAAS 是否有新版本（GitHub Releases API；失败/无新版返回 None，不阻塞启动）

        返回 {"local", "latest", "compatible", "url", "release_notes"}：
        - 仅比较 stable release（跳过 prerelease/draft）
        - compatible：最新 stable 与本地是否同主版本线（BAAS-Plus 依赖 BAAS 的
          core/ 结构，重构版（如 1.5+ 新结构）可能不兼容，只提示不推荐自动更新）
        """
        local = self.get_local_baas_version()
        if not local:
            return None
        try:
            import urllib.request

            req = urllib.request.Request(
                "https://api.github.com/repos/pur1fying/blue_archive_auto_script/"
                "releases?per_page=10",
                headers={
                    "User-Agent": "BAAS-Plus",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                releases = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            logger.debug("检查 BAAS 更新失败: %s", exc)
            return None
        stable = [r for r in releases if not r.get("prerelease") and not r.get("draft")]
        if not stable:
            return None
        latest = stable[0]
        tag = (latest.get("tag_name") or "").lstrip("vV")

        def _num(version: str) -> tuple[int, ...]:
            parts = re.findall(r"\d+", version)[:3]
            return tuple(int(p) for p in parts) or (0,)

        local_num, latest_num = _num(local), _num(tag)
        if not latest_num or latest_num <= local_num:
            return None
        return {
            "local": local,
            "latest": tag,
            "compatible": latest_num[0] == local_num[0],
            "url": latest.get("html_url", ""),
            "release_notes": (latest.get("body") or "")[:200],
        }

    def get_current_activity(self) -> str | None:
        """读取 BAAS 记录的活动模块名（current_game_activity）

        优先运行时 Baas_thread 属性（BAAS 扫荡实际消费的值）；否则纯文件读
        config.json 中的记录（无需 Baas_thread）。无记录返回 None。
        """
        if self.baas_thread is not None:
            value = getattr(self.baas_thread, "current_game_activity", None)
            if value:
                return value
        data = self._read_baas_config_file()
        if data is not None:
            value = data.get("current_game_activity")
            if isinstance(value, str) and value:
                return value
        return None

    def set_current_activity(self, module_name: str) -> None:
        """设置 BAAS 当前活动模块（current_game_activity）

        必须同时改 Baas_thread.current_game_activity 属性：BAAS 的
        sweep_activity 读的是该属性（init_device 时从 static_config 拷贝），
        只写 config 不生效。
        """
        if self.baas_thread is not None:
            self.baas_thread.current_game_activity = module_name
        try:
            self._config_set().set("current_game_activity", module_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 config current_game_activity 失败（可忽略）: %s", exc)
        logger.info("已设置 BAAS 活动模块: %s", module_name)

    def list_activity_modules(self) -> list[str]:
        """扫描当前服可用的活动模块白名单（BAAS 按服加载截图模板资源）

        注意：BAAS 的 position.init_image_data 只加载当前服
        src/images/{CN|Global|JP}/x_y_range/activity/ 下的模板，缺少模板的活动
        模块即使存在也会导致资源初始化失败（截图匹配全废）。因此这里扫的是
        **当前服的 x_y_range 资源目录**，而不是 module/activities/（全服共享）。
        """
        import os

        identifier = {"cn": "CN", "in": "Global", "jp": "JP"}.get(
            self.config.baas.server, "CN"
        )
        try:
            import_baas(self.config.baas.repo_dir)
            import module.activities as acts_pkg

            root = os.path.dirname(os.path.dirname(os.path.dirname(acts_pkg.__file__)))  # BAAS 根目录
            d = os.path.join(root, "src", "images", identifier, "x_y_range", "activity")
            if not os.path.isdir(d):
                logger.warning("当前服活动资源目录不存在: %s", d)
                return []
            # 同时校验关卡数据 JSON 存在（activity_utils 扫荡时需要；命名有两种：<模块>.json / <模块>.py.json）
            json_dir = os.path.join(root, "src", "explore_task_data", "activities")
            return sorted(
                f[:-3]
                for f in os.listdir(d)
                if f.endswith(".py")
                and not f.startswith("_")
                and (
                    os.path.exists(os.path.join(json_dir, f[:-3] + ".json"))
                    or os.path.exists(os.path.join(json_dir, f[:-3] + ".py.json"))
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("扫描 BAAS 活动模块失败: %s", exc)
            return []

    def activity_module_available(self, module_name: str) -> bool:
        """模块是否在当前服资源白名单内（防止选了缺模板的活动导致 BAAS 崩溃）"""
        return module_name in self.list_activity_modules()

    def _activity_resource_root(self) -> str | None:
        """BAAS 根目录（从 module.activities 包推导），失败返回 None"""
        try:
            import_baas(self.config.baas.repo_dir)
            import module.activities as acts_pkg

            return os.path.dirname(os.path.dirname(os.path.dirname(acts_pkg.__file__)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("推导 BAAS 根目录失败: %s", exc)
            return None

    def _activity_template_path(self, module: str, filename: str) -> str | None:
        """BAAS 活动模块模板图路径：<root>/src/images/<identifier>/activity/<module>/<filename>"""
        root = self._activity_resource_root()
        if not root:
            return None
        identifier = {"cn": "CN", "in": "Global", "jp": "JP"}.get(
            self.config.baas.server, "CN"
        )
        path = os.path.join(
            root, "src", "images", identifier, "activity", module, filename
        )
        return path if os.path.isfile(path) else None

    def match_banner_activity(
        self, candidates: list[str], threshold: float = 0.8
    ) -> str | None:
        """模板匹配轮播图当前页活动

        用 BAAS 活动模块自带的 enter1.png（主页轮播图「进入活动」按钮模板，
        各活动样式不同）在轮播图区域搜索，绕开 OCR 对艺术字标题识别率低的问题。

        返回最高分且达阈值的模块名；无候选/无模板/无截图返回 None。
        """
        if not candidates or self.baas_thread is None:
            logger.warning(
                "轮播图模板匹配跳过：%s",
                "无候选活动" if not candidates else "baas_thread 未初始化（create_baas 未成功）",
            )
            return None
        update = getattr(self.baas_thread, "update_screenshot_array", None)
        if callable(update):
            update()
        img = getattr(self.baas_thread, "latest_img_array", None)
        if img is None:
            logger.warning("轮播图模板匹配跳过：无截图帧")
            return None
        region = tuple(self.config.baas.banner_region or [1109, 133, 1280, 281])
        ratio = getattr(self.baas_thread, "ratio", 1.0) or 1.0
        banner = img[
            int(region[1] * ratio) : int(region[3] * ratio),
            int(region[0] * ratio) : int(region[2] * ratio),
        ]
        if banner.size == 0:
            logger.warning(
                "轮播图模板匹配跳过：轮播图区域裁剪为空（banner_region=%s）",
                list(region),
            )
            return None
        import cv2

        best_mod, best_val = None, 0.0
        for mod in candidates:
            tpl_path = self._activity_template_path(mod, "enter1.png")
            if not tpl_path:
                logger.warning("活动模块 %s 无 enter1 模板，跳过模板匹配", mod)
                continue
            tpl = cv2.imread(tpl_path)
            if tpl is None:
                logger.warning("读取模板失败: %s", tpl_path)
                continue
            if tpl.shape[0] > banner.shape[0] or tpl.shape[1] > banner.shape[1]:
                logger.warning("活动模块 %s 模板大于轮播图区域，跳过", mod)
                continue
            res = cv2.matchTemplate(banner, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, _ = cv2.minMaxLoc(res)
            logger.info("轮播图模板匹配 %s: %.3f", mod, mx)
            if mx > best_val:
                best_val, best_mod = mx, mod
        if best_mod and best_val >= threshold:
            logger.info("轮播图模板匹配到目标活动: %s (%.3f)", best_mod, best_val)
            return best_mod
        if best_mod:
            logger.info(
                "轮播图模板匹配未达阈值: 最高 %s %.3f < %.2f",
                best_mod,
                best_val,
                threshold,
            )
        return None

    def ocr_banner(self) -> str:
        """OCR 主页轮播图区域文字（复用 BAAS 的 zh-cn OCR）；失败返回空串

        优化点：
        1. OCR 前先刷新截图——BAAS 的 OCR 读 latest_img_array 缓存帧，不刷新
           会识别旧画面（甚至 None），导致"识别不出东西"
        2. 区域裁剪后放大 2x + CLAHE 增强再识别（艺术字/小字号识别率低）
        3. 返回原始识别文本，不做 is_chinese_char 中文过滤——艺术字常被识别
           成 l/1/o/0 等非中文字符，纯中文过滤会把结果滤光成空串
        """
        if self.baas_thread is None or self.baas_thread.ocr is None:
            return ""
        region = tuple(self.config.baas.banner_region or [1109, 133, 1280, 281])
        try:
            update = getattr(self.baas_thread, "update_screenshot_array", None)
            if callable(update):
                update()
            img = getattr(self.baas_thread, "latest_img_array", None)
            if img is None:
                logger.warning("轮播图 OCR 跳过：无截图帧")
                return ""
            ratio = getattr(self.baas_thread, "ratio", 1.0) or 1.0
            crop = img[
                int(region[1] * ratio) : int(region[3] * ratio),
                int(region[0] * ratio) : int(region[2] * ratio),
            ]
            if crop.size == 0:
                return ""
            import cv2

            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
            gray = cv2.resize(
                gray,
                (gray.shape[1] * 2, gray.shape[0] * 2),
                interpolation=cv2.INTER_CUBIC,
            )
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            text = self.baas_thread.ocr.ocr_for_single_line(
                language="zh-cn",
                log_info="banner",
                origin_image=gray,
                pass_method=1,
                shared_memory_name="",
                _logger=getattr(self.baas_thread, "logger", None),
            )
            text = text or ""
            logger.info("轮播图 OCR 原始文本: %r", text)
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 轮播图区域失败: %s", exc)
            return ""

    def set_sweep_tasks(self, normal_tasks: list[str], hard_tasks: list[str]) -> None:
        """设置普通/困难图扫荡列表（mainlinePriority / hardPriority，格式 region-mission-counts）

        BAAS 这两个字段是 **str 类型**（逗号分隔），必须 join 成字符串写入；
        直接写 list 会把 config.json 变成 JSON 数组，下次 str(list)+split 读取
        产生嵌套引号污染（每次运行加深一层）。设置后重新解析 unfinished 任务
        列表（BAAS 扫荡任务实际消费的数据）。
        """
        normal_str = ",".join(normal_tasks)
        hard_str = ",".join(hard_tasks)
        # 纯文件写入优先（无需 Baas_thread）；写入成功即返回，不刷新内存态
        data = self._read_baas_config_file()
        if data is not None:
            data["mainlinePriority"] = normal_str
            data["hardPriority"] = hard_str
            if self._write_baas_config_file(data):
                logger.info(
                    "已写入 BAAS 扫荡列表(文件): normal=%s hard=%s",
                    normal_tasks,
                    hard_tasks,
                )
                return
        config_set = self._config_set()
        config_set.set("mainlinePriority", normal_str)
        config_set.set("hardPriority", hard_str)
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
        # to_main_page 的 co_detect 结束条件是右上角 main_page RGB 特征，公告弹窗
        # （news 等）在屏幕中部/左上时不遮挡该特征 → co_detect 提前返回、弹窗残留；
        # BAAS 官方由各任务模块自己合并 GAME_ONE_TIME_POP_UPS 处理弹窗，任务列表
        # 为空时没有模块执行，这里主动检测并关闭，避免后续轮播图识别被弹窗干扰
        self.close_announcement_popups(timeout=30)

    def close_announcement_popups(self, timeout: float = 30.0) -> bool:
        """检测并关闭 BA 的一次性公告弹窗（news / 公告栏等）

        BAAS 的 to_main_page() 用 co_detect 以 main_page RGB 特征（右上角）作为
        结束条件，弹窗不遮挡该区域时会提前返回；BAAS 官方任务模块（arena 等）
        会把 picture.GAME_ONE_TIME_POP_UPS 合并进 img_reactions 处理，任务列表
        为空时无人处理。这里复用 BAAS 的弹窗特征表主动检测并点击关闭。

        返回 True 表示确认无弹窗残留（或已全部关闭）；False 表示超时仍有弹窗。
        """
        if self.baas_thread is None:
            return False
        baas = self.baas_thread
        try:
            import_baas(self.config.baas.repo_dir)
            from core import image
            from core.picture import GAME_ONE_TIME_POP_UPS
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载 BAAS 弹窗特征失败，跳过公告弹窗处理: %s", exc)
            return False
        server = getattr(baas, "server", "CN")
        popups = GAME_ONE_TIME_POP_UPS.get(server, {})
        if not popups:
            logger.info("无公告弹窗特征表（server=%s）", server)
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                baas.update_screenshot_array()
            except Exception as exc:  # noqa: BLE001
                logger.warning("刷新截图失败，终止公告弹窗检测: %s", exc)
                break
            hit = None
            for feat, click in popups.items():
                try:
                    if image.compare_image(baas, feat):
                        hit = (feat, click)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if hit is None:
                logger.info("未检测到公告弹窗，主界面干净")
                return True
            feat, (x, y) = hit
            logger.info("检测到公告弹窗 %s，点击 (%d,%d) 关闭", feat, x, y)
            try:
                baas.click(x, y)
            except Exception as exc:  # noqa: BLE001
                logger.warning("点击关闭弹窗 %s 失败: %s", feat, exc)
                break
            time.sleep(1.5)
        logger.warning("等待公告弹窗关闭超时（%.0fs），仍有弹窗可能残留", timeout)
        return False

    def click_banner_enter(self) -> None:
        """立即点击轮播图当前页的「进入活动」按钮（activity_enter1 @ (1196,195)）

        模板匹配已确认轮播图当前页 = 目标活动时直接点击，跳过 BAAS
        to_activity() 内部 co_detect 的轮询延迟（每轮 sleep 1.5s，轮播图可能
        在此期间轮走，导致点击落在错误活动上）。坐标与 BAAS picture.activity_enter1
        一致（1280x720 基准，baas.click 内部会做分辨率缩放）。
        """
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        self.baas_thread.click(1196, 195)
        logger.info("BAAS-Plus 立即点击 activity_enter1 (1196,195)（跳过 BAAS 特征轮询）")

    def enter_current_activity(self, timeout: float = 8.0) -> bool:
        """点击 enter1 进入当前轮播图页的活动，并等待 activity_menu 出现

        模板匹配已确认轮播图当前页 = 目标活动时调用（enter1 按钮位置固定，
        直接点击比 BAAS co_detect 轮询快，避免轮播图轮走）。返回 True 表示
        已进入活动菜单（activity_menu 特征出现）。
        """
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        baas = self.baas_thread
        self.click_banner_enter()
        try:
            import_baas(self.config.baas.repo_dir)
            from core import image
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载 BAAS 图像模块失败，无法确认进入活动菜单: %s", exc)
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                baas.update_screenshot_array()
            except Exception as exc:  # noqa: BLE001
                logger.warning("刷新截图失败，终止活动菜单检测: %s", exc)
                break
            try:
                if image.compare_image(baas, "activity_menu"):
                    logger.info("已进入活动菜单 activity_menu")
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        logger.warning("点击 enter1 后 %.0fs 内未检测到 activity_menu，可能未进入活动", timeout)
        return False

    def go_main_page(self) -> None:
        """真实执行 BAAS to_main_page()（处理弹窗/回主页），供推图后复位使用"""
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        self.baas_thread.to_main_page()
        logger.info("BAAS-Plus 已回到主界面")

    def _ocr_region(
        self, region: tuple[int, int, int, int], enlarge: int = 2
    ) -> str:
        """OCR 指定区域（720p 基准坐标）；复用轮播图 OCR 预处理（刷新截图+放大+CLAHE）"""
        if self.baas_thread is None or self.baas_thread.ocr is None:
            return ""
        try:
            update = getattr(self.baas_thread, "update_screenshot_array", None)
            if callable(update):
                update()
            img = getattr(self.baas_thread, "latest_img_array", None)
            if img is None:
                return ""
            ratio = getattr(self.baas_thread, "ratio", 1.0) or 1.0
            x1, y1, x2, y2 = region
            crop = img[
                int(y1 * ratio) : int(y2 * ratio),
                int(x1 * ratio) : int(x2 * ratio),
            ]
            if crop.size == 0:
                return ""
            import cv2

            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
            gray = cv2.resize(
                gray,
                (gray.shape[1] * enlarge, gray.shape[0] * enlarge),
                interpolation=cv2.INTER_CUBIC,
            )
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            text = self.baas_thread.ocr.ocr_for_single_line(
                language="zh-cn",
                log_info="region",
                origin_image=gray,
                pass_method=1,
                shared_memory_name="",
                _logger=getattr(self.baas_thread, "logger", None),
            )
            return (text or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 区域失败 %s: %s", region, exc)
            return ""

    def detect_mission_attribute(self) -> str | None:
        """在任务详情页点击「敌人/克制」，OCR 第一个敌人的防御类型 → 返回克制属性

        坐标（1280x720 基准，BAAS 自动按分辨率缩放）：
        - 敌人/克制按钮: (213, 274)
        - 第一个敌人防御类型文字区域: (346, 387)-(402, 411)
        - 弹窗关闭按钮: (1065, 105)

        防御类型白名单（子串匹配，游戏内 6 种装甲，克制按游戏内表格）：一般→爆发 /
        轻→爆发 / 重→贯穿 / 特殊→神秘 / 弹力→震动；复合装甲对应 BAAS 暂不支持的
        「分解」属性，识别到后不覆盖（返回 None，用 BAAS 原 JSON 数据）。识别失败
        同样返回 None，不阻塞流程。
        """
        if self.baas_thread is None:
            return None
        baas = self.baas_thread
        text = ""
        try:
            baas.click(213, 274)
            for _ in range(6):  # 最多 ~5s 等弹窗加载 + OCR
                time.sleep(0.8)
                text = self._ocr_region((346, 387, 402, 411))
                for armor, attr in MISSION_ARMOR_MAP.items():
                    if armor in text:
                        logger.info(
                            "任务敌人防御类型「%s」→ 使用 %s 队", armor, attr
                        )
                        time.sleep(0.3)
                        baas.click(1065, 105)  # 关闭弹窗
                        time.sleep(0.6)  # 等关闭动画结束，避免残留遮挡
                        return attr
            logger.warning("任务敌人属性识别失败（OCR 文本: %r）", text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务敌人属性检测异常: %s", exc)
        try:  # 保底关闭弹窗，避免残留遮挡后续流程
            baas.click(1065, 105)
        except Exception:  # noqa: BLE001
            pass
        return None

    def solve_activity_sweep_after_enter(self) -> Any:
        """已在目标活动菜单内时执行 activity_sweep：临时屏蔽 to_main_page

        BAAS 的 activity_sweep() 开头强制调用 self.to_main_page()（内部点
        main_page_quick-home @ (1236,31) 退回主页），会把已进入的活动页退出，
        随后 to_activity 再点 enter1 时轮播图已轮走（可能进错活动）。
        这里临时把实例的 to_main_page 替换为 no-op，让 BAAS 从活动菜单直接
        继续：to_activity 的 co_detect 先查 end 特征 activity_menu（已出现）
        → 立即通过，后续选任务/AP 计算/扫荡/弹窗处理全部复用 BAAS 原生逻辑。
        """
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        baas = self.baas_thread
        orig = baas.to_main_page
        try:
            baas.to_main_page = lambda: None  # type: ignore[method-assign]
            logger.info("已屏蔽 to_main_page，执行 BAAS activity_sweep（活动菜单内）")
            return self.solve("activity_sweep")
        finally:
            baas.to_main_page = orig

    def solve_activity_explore_mission(self) -> Any:
        """已在目标活动菜单内时执行活动推图 explore_activity_mission

        推图（打通任务至全 SSS）与扫荡共用 to_mission_task_info 定位，BAAS 的
        explore_activity_mission() 开头同样强制 to_main_page()，这里同样屏蔽，
        让 BAAS 从活动菜单直接开始推图（已 SSS 的关卡会快速跳过）。

        同时 patch start_mission：BAAS 的关卡属性数据（explore_task_data JSON）
        为开发者手动录入，可能与实际关卡不符（如笑笑闹闹 JSON 全写 shock，
        实际关卡是爆发）。每次 start_mission 前点击「敌人/克制」实机 OCR 第一个
        敌人的防御类型，用真实属性覆盖后再进编队，避免选错队伍。
        """
        if self.baas_thread is None:
            raise RuntimeError("Baas_thread 未初始化")
        baas = self.baas_thread
        orig_main = baas.to_main_page
        orig_start = None
        patched_module = None
        try:
            baas.to_main_page = lambda: None  # type: ignore[method-assign]
            import module.activities.activity_utils as au

            patched_module = au
            orig_start = au.start_mission

            def patched_start_mission(instance, attribute_str: str):
                real = self.detect_mission_attribute()
                if real:
                    logger.info(
                        "start_mission 属性修正: %s -> %s", attribute_str, real
                    )
                    attribute_str = real
                return orig_start(instance, attribute_str)

            au.start_mission = patched_start_mission
            logger.info(
                "已屏蔽 to_main_page + 已安装敌人属性检测，执行 BAAS explore_activity_mission（活动内推图）"
            )
            return self.solve("explore_activity_mission")
        finally:
            baas.to_main_page = orig_main
            if orig_start is not None and patched_module is not None:
                patched_module.start_mission = orig_start

    def sync_sweep_from_baas(self) -> dict[str, Any]:
        """BAAS ↔ BAAS-Plus 扫荡列表双向同步（纯文件读写，无需 Baas_thread/模拟器）

        供 WebUI 保存「模拟器&BAAS」设置后调用（用户确认动作）：
        1. 从 BAAS config.json 读取扫荡列表，BAAS-Plus 对应项为空时填充
        2. BAAS-Plus 已有配置且与 BAAS 不同时，写回 BAAS config.json
        返回同步结果供前端提示。
        """
        if not self.config.baas.repo_dir:
            return {"ok": False, "reason": "baas.repo_dir 为空，无法读取 BAAS 配置"}
        baas_cfg = self.get_baas_sweep_config()
        normal = baas_cfg["mainlinePriority"]
        hard = baas_cfg["hardPriority"]
        changed = False
        if not self.config.sweep.normal_tasks and normal:
            self.config.sweep.normal_tasks = normal
            changed = True
        if not self.config.sweep.hard_tasks and hard:
            self.config.sweep.hard_tasks = hard
            changed = True
        # 写回：BAAS-Plus 已有配置时同步到 BAAS（保存配置 = 用户确认）
        pushed = False
        if self.config.sweep.normal_tasks or self.config.sweep.hard_tasks:
            if (
                self.config.sweep.normal_tasks != normal
                or self.config.sweep.hard_tasks != hard
            ):
                self.set_sweep_tasks(
                    self.config.sweep.normal_tasks,
                    self.config.sweep.hard_tasks,
                )
                pushed = True
        return {
            "ok": True,
            "normal_tasks": normal,
            "hard_tasks": hard,
            "applied": changed,
            "pushed": pushed,
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
