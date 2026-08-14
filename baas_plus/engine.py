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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .activity import ActivityFetcher, EventType, GameEvent
from .baas_bridge import BaasBridge, compute_sweep_times
from .config import AppConfig, SWEEP_TASKS
from .notifier import EmailNotifier
from .store import Store

logger = logging.getLogger(__name__)

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

    # ---- 活动检测 ----

    async def detect_new_activities(self) -> list[GameEvent]:
        """拉取活动并返回未处理过的新活动（同时更新本地状态为已见）"""
        events = await self.fetcher.fetch_all()
        new_events = [e for e in events if not self.store.is_activity_seen(e)]
        for event in events:
            self.store.mark_activity_seen(event)
        # 活动类新事件记录为已见；若将触发推图，稍后更新为 pushed
        logger.info("活动检测: 共 %d 个事件，新事件 %d 个", len(events), len(new_events))
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

    def run_sweep(self) -> list[str]:
        """扫荡阶段：返回实际执行的扫荡任务

        BAAS-Plus 配置的扫荡列表为空时，回退读取 BAAS 配置中的
        mainlinePriority / hardPriority（格式一致："区域-关卡-次数"）。
        """
        swept: list[str] = []
        ap = self.bridge.get_ap()
        self.result.ap_before_sweep = ap
        if ap <= 0:
            logger.warning("读取体力失败或体力为 0，跳过扫荡")
            return swept

        activity_active = self.has_active_activity()

        if activity_active and self.config.sweep.activity_first:
            # 活动扫荡：-1 = BAAS 按当前 AP 自动计算最大次数
            self.bridge.set_activity_sweep(
                self.config.sweep.activity_task_number, times="-1"
            )
            self.bridge.solve("activity_sweep")
            swept.append(f"activity:{self.config.sweep.activity_task_number}(auto)")
            ap = self.bridge.get_ap()

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

    def _run_task_checked(self, task: str) -> None:
        """执行单个勾选任务并记录；arena 自动重复至票用完（含冷却等待）"""
        import time

        if task == "arena":
            # BAAS arena 模块：票数 >1 时打一场后设置 next_time（默认 55s 冷却）
            # 请求调度器再次执行，直到票用完（next_time 归 0）。BAAS-Plus 不走
            # scheduler 主循环，这里手动模拟重复执行，最多 6 场（5 票 + 容差）。
            for i in range(6):
                self.bridge.solve("arena")
                self.result.executed_tasks.append("arena")
                cooldown = self.bridge.last_next_time
                if cooldown <= 0:
                    break
                logger.info("竞技场冷却 %ss，等待后继续（第 %s 场）", cooldown, i + 2)
                time.sleep(cooldown)
            return
        self.bridge.solve(task)
        self.result.executed_tasks.append(task)

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

            # 3. 启动游戏并确保在主界面（BAAS 官方主循环的第一步 restart）
            try:
                self.bridge.solve("restart")
                self.result.executed_tasks.append("restart")
            except Exception as exc:  # noqa: BLE001
                logger.error("启动游戏失败: %s", exc)
                self.result.status = "partial"

            # 4. 勾选任务（扫荡类任务由扫荡阶段统一调度，避免重复执行）
            for task in self.config.baas.tasks:
                if task in SWEEP_TASKS:
                    logger.info("跳过任务 %s（由扫荡阶段按体力统一调度）", task)
                    continue
                try:
                    self._run_task_checked(task)
                except Exception as exc:  # noqa: BLE001
                    logger.error("任务 %s 执行失败: %s", task, exc)
                    self.result.status = "partial"

            # 5. 扫荡
            self.result.swept = self.run_sweep()

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
