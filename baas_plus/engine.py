"""核心引擎：一次执行的完整编排

流程：
1. 启动模拟器（Mumu 等）获取 ADB 端口 → 初始化 BAAS
2. 活动检测：拉取 GameKee 活动 → 对比本地状态 → 发现未处理的新活动
3. 新活动按配置触发活动推图（explore_activity_story / mission / challenge）
4. 依次执行勾选的日常任务
5. 全部任务完成后读取剩余体力，计算扫荡次数：
   - 有活动且 activity_first → 先扫活动关卡（BAAS activity_sweep，-1 = 按 AP 自动）
   - 无活动（或配置）→ 扫普通/困难图（auto 模式下按剩余体力重算每关次数）
6. 写入执行记录 + 邮件通知
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from .activity import ACTIVITY_MODULE_ALIASES, ActivityFetcher, EventType, GameEvent
from .baas_bridge import BaasBridge, SWEEP_ITEM_RE, compute_sweep_times
from .config import AppConfig, SWEEP_TASKS
from .notifier import EmailNotifier
from .store import Store

logger = logging.getLogger(__name__)

# 领取类任务：在所有任务（含 arena、扫荡）执行完成后执行
AFTER_ALL_TASKS = ("collect_reward",)

if TYPE_CHECKING:
    from .store import Store as StoreType

# 普通图 / 困难图扫荡单次体力消耗
NORMAL_SWEEP_AP_COST = 10
HARD_SWEEP_AP_COST = 20
HARD_MAX_TIMES = 3  # BAAS 困难图单关上限

# 点开活动后若弹出「活动时间已结束」（轮播图换页瞬间点到已结束的活动），
# 返回主界面重新等待轮播图并再次尝试进入的最大次数；多次失败视为活动已结束，跳过扫荡
ACTIVITY_ENTER_MAX_RETRIES = 3


@dataclass
class RunResult:
    """一次执行的结果摘要"""

    status: str = "success"  # success / partial / failed
    new_activities: list[GameEvent] = field(default_factory=list)
    pushed_activities: list[str] = field(default_factory=list)
    executed_tasks: list[str] = field(default_factory=list)
    swept: list[str] = field(default_factory=list)
    ap_before_sweep: int = -1
    summary: str = ""


class Engine:
    def __init__(
        self,
        config: AppConfig,
        store: StoreType | None = None,
        bridge: BaasBridge | None = None,
        fetcher: ActivityFetcher | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.data_path / "baas_plus.db")
        self.bridge = bridge or BaasBridge(config)
        self.fetcher = fetcher or ActivityFetcher(config.activity.server)
        self.result = RunResult()
        # arena 与常规任务并发执行时，用锁保证同一时刻仅一个任务操作 BAAS
        self._baas_lock = threading.Lock()
        self._update_checked = False

    # ---- 活动检测 ----

    async def detect_new_activities(self) -> list[GameEvent]:
        """拉取活动并返回未处理过的新活动（同时更新本地状态为已见）

        只关注已开始且未结束的活动：未开始的（预告/尚未开启）不推送、
        不标记已见，等活动正式开始后的下一次执行再检测并推送。
        """
        events = await self.fetcher.fetch_all()
        now = _now()
        # start_at 缺失（0）视为已开始，避免误杀无开始时间的活动
        active = [e for e in events if e.start_at <= now <= e.end_at]
        new_events = [e for e in active if not self.store.is_activity_seen(e)]
        for event in active:
            self.store.mark_activity_seen(event)
        logger.info(
            "活动检测: 共 %d 个事件，进行中 %d 个，新事件 %d 个（未开始/已结束不推送）",
            len(events), len(active), len(new_events),
        )
        return new_events

    def resolve_activity_module(self, event: GameEvent) -> str | None:
        """将活动事件映射到 BAAS 活动模块名（module/activities/<name>.py）

        优先级：手动配置 current_activity > BAAS 记录的 current_game_activity
        （BAAS 记录可能是 BAAS 自己检测或之前运行写入的，同样可信）。
        """
        if self.config.baas.current_activity:
            manual = self.config.baas.current_activity
            if self.bridge.activity_module_available(manual):
                return manual
            logger.warning(
                "手动配置的活动模块 %s 不在当前服资源白名单（缺少 BAAS 截图模板），跳过推图",
                manual,
            )
            return None
        recorded = self.bridge.get_current_activity()
        if recorded and self.bridge.activity_module_available(recorded):
            logger.info("使用 BAAS 记录的活动模块: %s", recorded)
            return recorded
        return None

    def push_new_activity(self, event: GameEvent) -> list[str]:
        """对新活动执行推图；返回实际执行的任务列表"""
        executed: list[str] = []
        module_name = self.resolve_activity_module(event)
        if module_name is None:
            logger.warning(
                "检测到新活动「%s」但无法确定 BAAS 活动模块，跳过自动推图"
                "（可在配置 baas.current_activity 或 WebUI 中指定）",
                event.title,
            )
            return executed

        self.bridge.set_current_activity(module_name)
        tasks = []
        if self.config.activity.push_story_on_new:
            tasks.append("explore_activity_story")
        if self.config.activity.push_mission_on_new:
            tasks.append("explore_activity_mission")
        if self.config.activity.push_challenge_on_new:
            tasks.append("explore_activity_challenge")
        for task in tasks:
            try:
                self.bridge.solve(task)
                executed.append(task)
            except Exception as exc:  # noqa: BLE001
                logger.error("活动推图任务 %s 失败: %s", task, exc)
        self.store.mark_activity_seen(event, pushed=True)
        return executed

    # ---- 扫荡 ----

    async def _select_sweep_activity(self) -> tuple[str, str | None] | None:
        """选择要扫荡的活动；返回 (模块名, 活动标题)，标题用于轮播图 OCR 关键词

        优先级：手动配置 current_activity > BAAS 记录的 current_game_activity
        > GameKee 进行中的活动启发式匹配
        （标题英文关键词 ↔ BAAS 模块名）。全部失败返回 None（跳过活动扫荡，
        避免扫到已结束/仅兑换可用的旧活动模块）。
        """
        if self.config.baas.current_activity:
            manual = self.config.baas.current_activity
            if self.bridge.activity_module_available(manual):
                return manual, None
            logger.warning(
                "手动配置的活动模块 %s 不在当前服资源白名单（缺少 BAAS 截图模板），"
                "跳过活动扫荡",
                manual,
            )
            return None
        recorded = self.bridge.get_current_activity()
        if recorded and self.bridge.activity_module_available(recorded):
            logger.info("活动扫荡选中模块（BAAS 记录）: %s", recorded)
            return recorded, None
        modules = self.bridge.list_activity_modules()
        if not modules:
            logger.warning("无法扫描 BAAS 活动模块列表")
            return None
        events = await self.fetcher.fetch_all()
        now = _now()
        for event in events:
            if event.event_type == EventType.EVENT and event.start_at <= now <= event.end_at:
                matched = self._match_activity_module(event.title, modules)
                if matched:
                    logger.info("活动扫荡选中模块: 「%s」 → %s", event.title, matched)
                    return matched, event.title
        return None

    @staticmethod
    def _match_activity_module(title: str, modules: list[str]) -> str | None:
        """标题英文关键词 → BAAS 活动模块启发式匹配

        GameKee 标题多为中文，BAAS 模块为英文名（如 CodeBox）；活动标题常含
        英文活动名关键词（如「CODE：BOX」→ CodeBox）。提取标题中的英文/数字词
        （>=3 字符），与模块名做子串匹配；纯中文标题靠
        ACTIVITY_MODULE_ALIASES 人工映射命中。
        """
        words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", title)
        norm_words = {w.lower() for w in words}
        for mod in modules:
            mn = mod.lower()
            if norm_words and any(w in mn for w in norm_words):
                return mod
        for keyword, mod in ACTIVITY_MODULE_ALIASES.items():
            if keyword in title and mod in modules:
                return mod
        return None

    @staticmethod
    def _banner_keywords(title: str) -> list[str]:
        """从活动标题提取轮播图 OCR 关键词（人工映射 key + 连续中文片段）"""
        kws: list[str] = []
        for kw in ACTIVITY_MODULE_ALIASES:
            if kw in title:
                kws.append(kw)
        cleaned = re.sub(r"[【】\[\]()（）复刻活动]", "", title or "")
        for w in re.findall(r"[\u4e00-\u9fff]{2,4}", cleaned):
            kws.append(w)
        return list(dict.fromkeys(kws))

    @staticmethod
    def _fuzzy_contains(ocr_text: str, keyword: str) -> bool:
        """keyword 是否近似出现在 OCR 文本中

        三层匹配：整词包含 → 单字命中率（艺术字常错字/漏字，≥60% 单字出现
        即命中）→ 滑动窗口 SequenceMatcher（容 OCR 错字）。
        """
        if not ocr_text or not keyword:
            return False
        if keyword in ocr_text:
            return True
        kw_chars = [c for c in keyword if c.strip()]
        if len(kw_chars) >= 2:
            hit = sum(1 for c in kw_chars if c in ocr_text)
            if hit / len(kw_chars) >= 0.6:
                return True
        n = len(keyword)
        if n < 2 or len(ocr_text) < n:
            return False
        best = 0.0
        for i in range(len(ocr_text) - n + 1):
            ratio = SequenceMatcher(None, ocr_text[i : i + n], keyword).ratio()
            if ratio > best:
                best = ratio
        return best >= 0.6

    async def _wait_for_activity_banner(
        self,
        keywords: list[str],
        module: str | None = None,
        timeout: float = 90.0,
    ) -> bool:
        """等待主页轮播图自动换页到目标活动

        优先模板匹配（BAAS 自带 enter1.png 认轮播图按钮，绕开 OCR 艺术字
        识别率低的问题）；OCR 模糊匹配作为兜底。

        BAAS 的 to_activity 点击硬编码坐标 (1196,195) 进入的是轮播图当前页的活动，
        因此必须先确认轮播图显示的是目标活动，再让 BAAS 进入。
        """
        logger.info(
            "等待轮播图到目标活动: module=%s keywords=%s（模板匹配优先，OCR 兜底）",
            module,
            keywords,
        )
        start = time.time()
        last_text = ""
        fail_polls = 0
        while time.time() - start < timeout:
            if module:
                hit = self.bridge.match_banner_activity([module])
                if hit:
                    logger.info("轮播图模板匹配到目标活动 %s", hit)
                    return True
            text = self.bridge.ocr_banner()
            if text:
                last_text = text
                if any(self._fuzzy_contains(text, kw) for kw in keywords):
                    logger.info("轮播图 OCR 识别到目标活动（%s）", text)
                    return True
            # 连续识别失败时周期性尝试关闭公告弹窗：BA 的公告弹窗（news 等）
            # 可能遮挡轮播图区域导致模板/OCR 都识别不到（BAAS to_main_page 的
            # RGB 结束特征不受弹窗影响，弹窗可能残留）；任务列表为空时没有
            # BAAS 任务模块代为处理，这里主动关闭
            fail_polls += 1
            if fail_polls % 5 == 0:
                logger.info(
                    "轮播图连续 %d 次未识别到目标活动，尝试关闭可能的公告弹窗...",
                    fail_polls,
                )
                try:
                    self.bridge.close_announcement_popups(timeout=10)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关闭公告弹窗异常: %s", exc)
            await asyncio.sleep(1)
        logger.warning(
            "轮播图等待超时（%.0fs），未识别到目标活动关键词 %s；最近一次 OCR 文本: %r。"
            "若 BA 存在公告弹窗（news 公告栏），已尝试自动关闭但仍未恢复，请确认弹窗未遮挡轮播图区域",
            timeout,
            keywords,
            last_text,
        )
        return False

    def has_active_activity(self) -> bool:
        """本地状态中是否存在进行中的活动类事件"""
        for row in self.store.list_activities(limit=500):
            if row["event_type"] == EventType.EVENT.value and row["end_at"] >= _now():
                return True
        return False

    def _build_sweep_list(self, tasks: list[str], ap: int, is_normal: bool) -> list[str]:
        """构造 BAAS 扫荡列表（region-mission-counts）；auto 模式按剩余体力重算次数"""
        base_cost = NORMAL_SWEEP_AP_COST if is_normal else HARD_SWEEP_AP_COST
        max_per_task = HARD_MAX_TIMES if not is_normal else self.config.sweep.max_times
        result: list[str] = []
        for item in tasks:
            if not SWEEP_ITEM_RE.fullmatch(item):
                # 历史嵌套转义污染项（如 ['3-3-3'）直接拒绝，避免再次写回 BAAS 配置
                logger.warning("扫荡配置格式错误（应为 region-mission-counts）: %s", item)
                continue
            parts = item.split("-")
            region, mission, counts = parts
            if self.config.sweep.strategy == "auto":
                times = compute_sweep_times(ap, base_cost, max_per_task)
                if times <= 0:
                    logger.info("体力不足，跳过扫荡 %s-%s", region, mission)
                    continue
                result.append(f"{region}-{mission}-{times}")
            else:
                result.append(f"{region}-{mission}-{counts if counts != 'max' else max_per_task}")
        return result

    async def run_sweep(self) -> list[str]:
        """扫荡阶段：返回实际执行的扫荡任务

        BAAS-Plus 配置的扫荡列表为空时，回退读取 BAAS 配置中的
        mainlinePriority / hardPriority（格式一致："区域-关卡-次数"）。
        活动扫荡：从 GameKee 进行中的活动里选择能映射到 BAAS 模块的活动，
        避免扫到已结束（仅兑换可用）的活动模块。
        """
        swept: list[str] = []
        ap = self.bridge.get_ap()
        if ap <= 0:
            # 读取体力失败：截图帧为空通常意味着模拟器/游戏失联（任务期间游戏闪退
            # 或模拟器无响应）。配置允许时重启一次模拟器再重试；仍失败则跳过扫荡，
            # 不中断后续的奖励领取/通知流程
            if self.config.simulator.auto_restart_on_failure:
                ap = await self._recover_ap()
        self.result.ap_before_sweep = ap
        if ap <= 0:
            logger.warning("读取体力失败或体力为 0，跳过扫荡")
            return swept

        if self.config.sweep.activity_first:
            # 活动扫荡：-1 = BAAS 按当前 AP 自动计算最大次数
            selected = await self._select_sweep_activity()
            if selected:
                module, title = selected
                self.bridge.set_current_activity(module)
                self.bridge.set_activity_sweep(
                    self.config.sweep.activity_task_number, times="-1"
                )
                # 轮播图导航：BAAS 点 enter 进入的是轮播图当前页的活动，
                # 先等自动换页到目标活动（模板匹配优先，OCR 兜底）再让 BAAS 进入
                keywords = self._banner_keywords(title) if title else []

                async def _enter_and_run(task: str) -> bool:
                    """确保在主界面 → 等轮播图 → 点击 enter1 进入活动菜单 → 屏蔽 to_main_page 执行任务

                    轮播图区域（banner_region）只在主界面存在：扫描前先回主界面，
                    否则上一任务遗留的页面（活动菜单/任务列表等）会让模板/OCR
                    读到错误内容，甚至 enter1 固定坐标点击落在无关按钮上。

                    点开活动后若 OCR 到「活动时间已结束」（轮播图换页瞬间点击
                    落在了已结束的活动上），返回主界面重新等待轮播图并再次尝试，
                    最多 ACTIVITY_ENTER_MAX_RETRIES 次；多次失败说明目标活动
                    确实已结束，跳过该活动扫荡（不回退 BAAS 原生扫荡，避免在
                    结束弹窗上反复点击卡死）。
                    """
                    for attempt in range(1, ACTIVITY_ENTER_MAX_RETRIES + 1):
                        self.bridge.go_main_page()
                        if not await self._wait_for_activity_banner(
                            keywords, module=module
                        ):
                            logger.warning("轮播图未就绪，跳过「%s」", task)
                            return False
                        if self.bridge.enter_current_activity():
                            if task == "explore_activity_mission":
                                self.bridge.solve_activity_explore_mission()
                            else:
                                self.bridge.solve_activity_sweep_after_enter()
                            return True
                        if self.bridge.activity_ended_popup():
                            logger.warning(
                                "点开活动后检测到「活动时间已结束」，返回主界面重新尝试"
                                "（第 %d/%d 次）",
                                attempt,
                                ACTIVITY_ENTER_MAX_RETRIES,
                            )
                            continue
                        logger.warning(
                            "未能确认进入活动菜单，回退 BAAS 原生 %s（可能进错活动）", task
                        )
                        self.bridge.solve(task)
                        return True
                    logger.warning(
                        "连续 %d 次尝试进入活动均提示「活动时间已结束」，活动已结束，跳过「%s」",
                        ACTIVITY_ENTER_MAX_RETRIES,
                        task,
                    )
                    return False

                if self.config.activity.push_before_sweep:
                    # 先推图（打通任务至全 SSS；已 SSS 的关卡快速跳过）：
                    # BAAS 定位任务靠按钮模板匹配，未解锁任务的按钮样式不匹配，
                    # 不推图直接扫荡会定位失败（swipe_search_target_str 返回 None）；
                    # 推图后页面停在活动内，下一次 _enter_and_run 会先回主界面
                    logger.info("活动扫荡前先推图: %s", title)
                    await _enter_and_run("explore_activity_mission")

                if not await _enter_and_run("activity_sweep"):
                    logger.warning("活动扫荡未执行「%s」", title)
                else:
                    swept.append(
                        f"activity:{self.config.sweep.activity_task_number}(auto,{module})"
                    )
                    ap = self.bridge.get_ap()
                    # solve_activity_sweep_after_enter 屏蔽了 to_main_page，
                    # 扫荡结束后 BA 停在扫荡结果页而非主界面；若这是本轮最后的
                    # 扫荡，显式回主界面，避免整轮结束后游戏停留在非主界面
                    self.bridge.go_main_page()
            else:
                logger.warning(
                    "活动优先已开启，但无法确定进行中活动的 BAAS 模块，跳过活动扫荡"
                )

        normal_tasks = self.config.sweep.normal_tasks
        hard_tasks = self.config.sweep.hard_tasks
        if not normal_tasks and not hard_tasks:
            # 回退读取 BAAS 配置里的扫荡列表（bridge 已解析为干净的 region-mission-counts 列表）
            try:
                baas_cfg = self.bridge.get_baas_sweep_config()
                normal_tasks = baas_cfg["mainlinePriority"]
                hard_tasks = baas_cfg["hardPriority"]
                logger.info("扫荡列表取自 BAAS 配置: normal=%s hard=%s", normal_tasks, hard_tasks)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 BAAS 扫荡配置失败: %s", exc)

        normal = self._build_sweep_list(normal_tasks, ap, is_normal=True)
        hard = self._build_sweep_list(hard_tasks, ap, is_normal=False)
        if normal:
            self.bridge.set_sweep_tasks(normal, hard)
            self.bridge.solve("normal_task")
            swept.extend(normal)
        if hard:
            # normal/hard 同时存在时也必须把 hard 列表设置进去（BAAS 按 hardPriority 消费）
            self.bridge.set_sweep_tasks(normal, hard)
            self.bridge.solve("hard_task")
            swept.extend(hard)
        return swept

    async def _recover_ap(self) -> int:
        """体力读取失败（模拟器/游戏失联）时：重启一次模拟器后重试读取

        重启模拟器 + 重新初始化 BAAS + 重启游戏约需 1-3 分钟，放后台线程执行
        避免阻塞事件循环。返回重试后的体力；重启失败或仍读取失败返回 -1。
        """
        logger.warning("读取体力失败，自动重启模拟器后重试（约需 1-3 分钟）...")
        try:
            ok = await asyncio.to_thread(self.bridge.restart_simulator)
        except Exception as exc:  # noqa: BLE001
            logger.error("重启模拟器异常: %s", exc)
            return -1
        if not ok:
            logger.warning("模拟器重启失败，跳过扫荡")
            return -1
        ap = self.bridge.get_ap()
        logger.info("模拟器重启后读取体力: %s", ap)
        return ap

    def _solve_locked(self, task: str) -> None:
        """带锁执行 BAAS 任务：同一时刻仅一个任务在跑（arena 与常规任务共享锁）"""
        with self._baas_lock:
            self.bridge.solve(task)

    async def _run_regular_tasks(self, tasks: list[str]) -> None:
        """顺序执行常规任务（线程池 + 锁；与 arena 协程并发，锁保证不同时操作 BAAS）"""
        for task in tasks:
            try:
                await asyncio.to_thread(self._solve_locked, task)
                self.result.executed_tasks.append(task)
            except Exception as exc:  # noqa: BLE001
                logger.error("任务 %s 执行失败: %s", task, exc)
                self.result.status = "partial"

    async def _run_arena(self) -> None:
        """战术对抗赛：自动重复到票用完；冷却等待期间让出事件循环，常规任务穿插执行

        竞技场每天固定 5 张挑战券（最多 5 场，不存在第 6 场）。BAAS arena 模块：
        票数 >1 时打一场后设置 next_time（默认 55s 冷却）；最后一票打完只领奖励、
        不设置 next_time。baas_bridge.solve 每次执行前会把 next_time 重置为 0
        （对齐 BAAS 官方调度器语义），因此 last_next_time<=0 即表示票已用完、
        循环终止。range(6) 仅是防御性上限，正常流程在 5 场后由 next_time 归零结束。
        这里模拟调度器的"冷却→再派发"，用 asyncio.sleep 而非 time.sleep，
        等待期间其他协程（常规任务）可运行。
        """
        for i in range(6):
            try:
                await asyncio.to_thread(self._solve_locked, "arena")
                self.result.executed_tasks.append("arena")
            except Exception as exc:  # noqa: BLE001
                logger.error("竞技场执行失败: %s", exc)
                self.result.status = "partial"
                break
            cooldown = self.bridge.last_next_time
            if cooldown <= 0:
                break
            logger.info("竞技场冷却 %ss，等待期间穿插执行其他任务（第 %s 场）", cooldown, i + 2)
            await asyncio.sleep(cooldown)

    # ---- 主流程 ----

    def _record_detail(self) -> dict:
        """执行结果转可 JSON 序列化 dict（供执行记录存储）"""
        detail = {
            "status": self.result.status,
            "summary": self.result.summary,
            "executed_tasks": self.result.executed_tasks,
            "swept": self.result.swept,
            "ap_before_sweep": self.result.ap_before_sweep,
            "new_activities": [e.__dict__ for e in self.result.new_activities],
            "pushed_activities": self.result.pushed_activities,
        }
        return detail

    async def run_once(self) -> RunResult:
        """执行一次完整流程（供 CLI / WebUI 手动触发 / 计划任务调用）"""
        self.result = RunResult()
        record_id = self.store.add_record("running", "执行开始")
        try:
            # 0. BAAS 更新检查（进程内只查一次，失败静默不阻塞启动）
            if not self._update_checked:
                self._update_checked = True
                try:
                    update = await asyncio.to_thread(self.bridge.check_baas_update)
                except Exception:  # noqa: BLE001
                    update = None
                if update:
                    if update["compatible"]:
                        logger.warning(
                            "检测到 BAAS 新版本 %s（当前 %s），建议更新：%s",
                            update["latest"],
                            update["local"],
                            update["url"],
                        )
                    else:
                        logger.warning(
                            "检测到 BAAS 新版本 %s（当前 %s），但主版本线不同，"
                            "可能不兼容 BAAS-Plus，请谨慎更新：%s",
                            update["latest"],
                            update["local"],
                            update["url"],
                        )
            # 1. 模拟器 + BAAS
            adb = self.bridge.start_simulator()
            self.bridge.create_baas(adb)

            # 2. 活动检测与推图
            new_events = await self.detect_new_activities()
            self.result.new_activities = new_events
            for event in new_events:
                if event.event_type == EventType.EVENT:
                    pushed = self.push_new_activity(event)
                    self.result.pushed_activities.extend(pushed)

            # 3. BAAS-Plus 用配置的 BA 包名显式启动游戏并进入主界面
            # （不走裸 solve('restart')：BA 已在前台时 restart 不会 to_main_page；
            #  launch_game = app_start(配置包名) + to_main_page，包名配置真正生效）
            try:
                self.bridge.launch_game()
                self.result.executed_tasks.append("restart")
            except Exception as exc:  # noqa: BLE001
                logger.error("启动游戏失败: %s", exc)
                self.result.status = "partial"

            # 4. 勾选任务（扫荡类任务由扫荡阶段统一调度）；arena 与常规任务穿插：
            #    arena 冷却等待（asyncio.sleep）期间让出事件循环，常规任务继续跑
            sweepless = [t for t in self.config.baas.tasks if t not in SWEEP_TASKS]
            # 活动推图已由「活动策略」配置统一调度（新活动自动推图），任务列表入口已
            # 移除；旧配置残留 explore_activity_* 时跳过并提示，避免重复推图
            deprecated_act = [t for t in sweepless if t.startswith("explore_activity")]
            if deprecated_act:
                logger.warning(
                    "任务列表中的活动推图项已移除（%s），活动推图请在「活动策略」中配置",
                    ",".join(deprecated_act),
                )
            sweepless = [t for t in sweepless if not t.startswith("explore_activity")]
            regular = [t for t in sweepless if t != "arena" and t not in AFTER_ALL_TASKS]
            after = [t for t in sweepless if t in AFTER_ALL_TASKS]
            if "arena" in sweepless:
                await asyncio.gather(self._run_regular_tasks(regular), self._run_arena())
            else:
                await self._run_regular_tasks(regular)

            # 5. 扫荡
            self.result.swept = await self.run_sweep()

            # 6. 领取类任务（collect_reward）在所有任务（含 arena、扫荡）之后执行，
            #    确保扫荡推进的奖励进度（活动任务目标/每日任务计数等）也能被领取
            await self._run_regular_tasks(after)

            self.result.summary = self._build_summary()
            self.store.finish_record(record_id, self.result.status, self.result.summary, self._record_detail())
        except Exception as exc:  # noqa: BLE001
            logger.exception("执行失败")
            self.result.status = "failed"
            self.result.summary = f"执行失败: {exc}"
            self.store.finish_record(record_id, "failed", self.result.summary, {"error": str(exc)})
        finally:
            self._notify()
            self.bridge.stop()
        return self.result

    def _build_summary(self) -> str:
        parts = [f"执行{'成功' if self.result.status != 'failed' else '失败'}"]
        if self.result.new_activities:
            parts.append(f"新活动 {len(self.result.new_activities)} 个")
        if self.result.pushed_activities:
            parts.append(f"活动推图: {','.join(self.result.pushed_activities)}")
        if self.result.executed_tasks:
            parts.append(f"任务 {len(self.result.executed_tasks)} 个: {','.join(self.result.executed_tasks)}")
        if self.result.swept:
            parts.append(f"扫荡 {len(self.result.swept)} 项（体力 {self.result.ap_before_sweep}）")
        return "；".join(parts)

    def _notify(self) -> None:
        if not self.config.notify.enabled:
            return
        notifier = EmailNotifier(self.config.notify.email)
        subject = f"BAAS-Plus 执行{'成功' if self.result.status != 'failed' else '失败'} ({self.config.baas.server})"
        body = f"{self.result.summary}\n\n详情:\n"
        body += "\n".join(f"- {t}" for t in self.result.executed_tasks)
        if self.result.swept:
            body += "\n\n扫荡明细:\n" + "\n".join(f"- {s}" for s in self.result.swept)
        notifier.send(subject, body)


def _now() -> int:
    import time

    return int(time.time())
