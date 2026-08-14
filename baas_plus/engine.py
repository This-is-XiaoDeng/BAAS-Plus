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
from typing import TYPE_CHECKING, Any

import httpx

from .activity import ACTIVITY_MODULE_ALIASES, ActivityFetcher, EventType, GameEvent
from .baas_bridge import BaasBridge, compute_sweep_times
from .config import AppConfig, SWEEP_TASKS
from .notifier import EmailNotifier
from .store import Store

logger = logging.getLogger(__name__)

# 领取类任务：在所有任务（含 arena）执行完成后执行
AFTER_ALL_TASKS = ("collect_reward",)

# DeepL 免费接口限流冷却时间（秒）：503 后一段时间内不再请求翻译
deeplx_cooldown = 600

if TYPE_CHECKING:
    from .store import Store as StoreType

# 普通图 / 困难图扫荡单次体力消耗
NORMAL_SWEEP_AP_COST = 10
HARD_SWEEP_AP_COST = 20
HARD_MAX_TIMES = 3  # BAAS 困难图单关上限


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

        优先级：手动配置 current_activity > 事件标题精确匹配（未来可扩展模糊匹配/映射表）
        """
        if self.config.baas.current_activity:
            return self.config.baas.current_activity
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

    async def _select_sweep_activity(self) -> str | None:
        """选择要扫荡的活动模块

        优先级：手动配置 current_activity > GameKee 进行中的活动启发式匹配
        （标题英文关键词 ↔ BAAS 模块名）。全部失败返回 None（跳过活动扫荡，
        避免扫到已结束/仅兑换可用的旧活动模块）。
        """
        if self.config.baas.current_activity:
            return self.config.baas.current_activity
        modules = self.bridge.list_activity_modules()
        if not modules:
            logger.warning("无法扫描 BAAS 活动模块列表")
            return None
        events = await self.fetcher.fetch_all()
        now = _now()
        for event in events:
            if event.event_type == EventType.EVENT and event.start_at <= now <= event.end_at:
                matched = self._match_activity_module(event.title, modules)
                via = None
                if not matched:
                    translated = await self._translate_title(event.title)
                    if translated:
                        matched = self._match_translated_en(translated, modules)
                        via = "DeepLX 翻译"
                if matched:
                    suffix = f"（{via}）" if via else ""
                    logger.info("活动扫荡选中模块%s: 「%s」 → %s", suffix, event.title, matched)
                    return matched
        return None

    async def _translate_title(self, title: str) -> str | None:
        """DeepLX 翻译标题为英文；失败/限流返回 None（不阻断流程）"""
        url = (self.config.baas.deeplx_url or "").strip()
        if not url:
            return None
        if time.time() < getattr(self, "_deeplx_cooldown_until", 0):
            return None
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    url.rstrip("/") + "/translate",
                    json={"text": title, "target_lang": "EN"},
                )
                payload = resp.json()
            if resp.status_code == 503 or payload.get("code") == 503:
                self._deeplx_cooldown_until = time.time() + deeplx_cooldown
                logger.warning("DeepLX 被 DeepL 限流，%s 秒内不再尝试翻译", deeplx_cooldown)
                return None
            data = payload.get("data")
            if isinstance(data, str) and data.strip():
                return data.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("DeepLX 翻译失败: %s", exc)
        return None

    @staticmethod
    def _match_translated_en(en_text: str, modules: list[str]) -> str | None:
        """对 DeepLX 翻译后的英文文本做模块名匹配（英文关键词子串匹配）"""
        words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", en_text)
        norm_words = {w.lower() for w in words}
        if not norm_words:
            return None
        for mod in modules:
            mn = mod.lower()
            if any(w in mn for w in norm_words):
                return mod
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
            if keyword in title:
                return mod
        return None

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
            parts = item.split("-")
            if len(parts) != 3:
                logger.warning("扫荡配置格式错误（应为 region-mission-counts）: %s", item)
                continue
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
        self.result.ap_before_sweep = ap
        if ap <= 0:
            logger.warning("读取体力失败或体力为 0，跳过扫荡")
            return swept

        if self.config.sweep.activity_first:
            # 活动扫荡：-1 = BAAS 按当前 AP 自动计算最大次数
            module = await self._select_sweep_activity()
            if module:
                self.bridge.set_current_activity(module)
                self.bridge.set_activity_sweep(
                    self.config.sweep.activity_task_number, times="-1"
                )
                self.bridge.solve("activity_sweep")
                swept.append(f"activity:{self.config.sweep.activity_task_number}(auto,{module})")
                ap = self.bridge.get_ap()
            else:
                logger.warning(
                    "活动优先已开启，但无法确定进行中活动的 BAAS 模块，跳过活动扫荡"
                )

        normal_tasks = self.config.sweep.normal_tasks
        hard_tasks = self.config.sweep.hard_tasks
        if not normal_tasks and not hard_tasks:
            # 回退读取 BAAS 配置里的扫荡列表（逗号分隔的 "区域-关卡-次数"）
            try:
                baas_cfg = self.bridge.get_baas_sweep_config()
                normal_tasks = [s.strip() for s in baas_cfg["mainlinePriority"].split(",") if s.strip()]
                hard_tasks = [s.strip() for s in baas_cfg["hardPriority"].split(",") if s.strip()]
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
        """战术对抗赛：最多 6 场；冷却等待期间让出事件循环，常规任务穿插执行

        BAAS arena 模块：票数 >1 时打一场后设置 next_time（默认 55s 冷却）；
        最后一票打完后 next_time 归 0。这里模拟调度器的"冷却→再派发"，
        用 asyncio.sleep 而非 time.sleep，等待期间其他协程（常规任务）可运行。
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
            #    arena 冷却等待（asyncio.sleep）期间让出事件循环，常规任务继续跑；
            #    领取类任务（collect_reward）在全部任务（含 arena）之后执行
            sweepless = [t for t in self.config.baas.tasks if t not in SWEEP_TASKS]
            regular = [t for t in sweepless if t != "arena" and t not in AFTER_ALL_TASKS]
            after = [t for t in sweepless if t in AFTER_ALL_TASKS]
            if "arena" in sweepless:
                await asyncio.gather(self._run_regular_tasks(regular), self._run_arena())
            else:
                await self._run_regular_tasks(regular)
            await self._run_regular_tasks(after)

            # 5. 扫荡
            self.result.swept = await self.run_sweep()

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
