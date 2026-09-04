"""多账号批量执行编排

串行逐个账号执行完整流程（模拟器 → 活动检测/推图 → 任务 → 扫荡），
账号间完全隔离：每个账号独立的 Engine / BaasBridge / 模拟器实例。
共享进程级资源：
- BAAS Main（OCR 服务器进程）：跨账号复用，避免重复拉起（create_baas 仅在
  _main 为 None 时新建，Runner 注入后取回继续传给下一个账号）；
- ActivityFetcher：按 server 缓存实例（同服活动数据只需拉取一次）。

多次执行（run_times）：全部账号执行完一轮后立即开始下一轮（无间隔），
循环配置的轮数；轮与轮之间同样复用 BAAS Main（OCR 服务器只起一个）。

失败隔离：单个账号异常不外抛，兜底为 failed 的 RunResult，不影响其余账号。
全部账号执行完成后统一发送一封汇总邮件（总耗时 + 每轮各账号结果 + 最终剩余体力）。
"""
from __future__ import annotations

import logging
import time
from html import escape as html_escape
from typing import Callable

from .activity import ActivityFetcher
from .baas_bridge import BaasBridge
from .config import AccountConfig, AppConfig
from .engine import Engine, RunResult
from .notifier import EmailNotifier
from .store import Store

logger = logging.getLogger(__name__)


class MultiAccountRunner:
    """账号级批量执行入口（CLI / WebUI 共用）

    bridge_factory / fetcher_factory 可注入（测试替换 FakeBridge / FakeFetcher）；
    缺省使用真实实现。bridge_factory 需支持 main 注入以共享 OCR 服务器。
    """

    def __init__(
        self,
        config: AppConfig,
        store: Store | None = None,
        bridge_factory: Callable[[AccountConfig], BaasBridge] | None = None,
        fetcher_factory: Callable[[str], ActivityFetcher] | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.data_path / "baas_plus.db")
        # 共享 BAAS Main（OCR 服务器进程）；首次为 None，create_baas 时新建并回填
        self._main = None
        # 按 server 缓存活动数据源（同服只建一个实例）
        self._fetchers: dict[str, ActivityFetcher] = {}
        self._bridge_factory = bridge_factory or (lambda acc: BaasBridge(acc, main=self._main))
        self._fetcher_factory = fetcher_factory or (lambda server: ActivityFetcher(server))

    # ---- 账号查找 ----

    def get_account(self, ref: str) -> AccountConfig:
        """按 id（精确）或 name（精确/包含，唯一时）查找账号；找不到抛 ValueError"""
        for acc in self.config.accounts:
            if acc.id == ref:
                return acc
        for acc in self.config.accounts:
            if acc.name == ref:
                return acc
        matches = [a for a in self.config.accounts if ref in a.name]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(f"{a.id}({a.name})" for a in self.config.accounts)
        raise ValueError(f"找不到账号 {ref!r}，可用账号: {names}")

    def enabled_accounts(self) -> list[AccountConfig]:
        return [a for a in self.config.accounts if a.enabled]

    # ---- 执行 ----

    def _new_engine(self, account: AccountConfig) -> Engine:
        """为账号创建独立 Engine/Bridge；注入共享 Main 与同服 fetcher"""
        bridge = self._bridge_factory(account)
        fetcher = self._fetchers.setdefault(account.activity.server, self._fetcher_factory(account.activity.server))
        return Engine(
            account,
            account_id=account.id,
            store=self.store,
            bridge=bridge,
            fetcher=fetcher,
            data_dir=str(self.config.data_path),
            capture_screenshot=self.config.notify.attach_game_screenshot,
        )

    async def run_account(self, ref: str) -> tuple[str, RunResult]:
        """执行单个账号；执行阶段异常兜底为 failed 结果（不向外抛，保证失败隔离）

        注意：账号解析（get_account）失败抛 ValueError，由调用方（CLI 外层 /
        WebUI resolve_account）处理，不在兜底范围内。
        """
        account = self.get_account(ref)
        logger.info(
            "===== 开始执行账号 [%s] (id=%s, 模拟器 %s 实例 %s) =====",
            account.name,
            account.id,
            account.simulator.type,
            account.simulator.instance,
        )
        engine = self._new_engine(account)
        try:
            result = await engine.run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("账号 [%s] 执行异常", account.name)
            result = RunResult(status="failed", summary=f"执行异常: {exc}")
        finally:
            # 把本次创建/复用的 Main 取回，共享给下一个账号（OCR 服务器进程只起一个）
            self._main = getattr(engine.bridge, "_main", None)
        logger.info("===== 账号 [%s] 执行结束: %s =====", account.name, result.status)
        return account.id, result

    async def run_accounts(
        self,
        accounts: list[AccountConfig],
        times: int | None = None,
    ) -> list[list[tuple[str, RunResult]]]:
        """对目标账号列表串行执行 times 轮（缺省用配置 run_times）

        多次执行语义：每轮按账号顺序执行一遍完整流程，一轮结束后**立即**
        开始下一轮（无间隔；轮间复用共享 BAAS Main，OCR 服务器进程只起一个）。
        返回 [[(account_id, RunResult), ...], ...]：外层每项为一轮。
        """
        rounds: list[list[tuple[str, RunResult]]] = []
        times = max(1, times or self.config.run_times)
        for round_no in range(1, times + 1):
            if times > 1:
                logger.info(
                    "========== 第 %d/%d 轮开始（一轮结束立即执行下一轮） ==========",
                    round_no,
                    times,
                )
            round_results: list[tuple[str, RunResult]] = []
            for account in accounts:
                round_results.append(await self.run_account(account.id))
            rounds.append(round_results)
            if times > 1 and round_no < times:
                logger.info("===== 第 %d/%d 轮完成，立即开始下一轮 =====", round_no, times)
        return rounds

    async def run_all(self, times: int | None = None) -> list[list[tuple[str, RunResult]]]:
        """串行执行全部启用账号，共执行 times 轮（缺省配置 run_times），统一发送汇总邮件

        返回 [[(account_id, RunResult), ...], ...]：外层每项为一轮。
        """
        start = time.monotonic()
        rounds = await self.run_accounts(self.enabled_accounts(), times)
        elapsed = time.monotonic() - start
        self._notify_summary(rounds, elapsed)
        return rounds

    def _to_html(self, lines: list[str]) -> str:
        """把纯文本汇总行转成简单的 HTML 邮件正文

        标题行加粗着色：账号块用 ━━ 分隔（深蓝），多轮执行的「第 N 轮」分节
        用 ══ 分隔（浅蓝），视觉上区分层级。
        """
        parts: list[str] = []
        for line in lines:
            if not line.strip():
                parts.append('<div style="height:6px;"></div>')
            elif line.startswith("═══") and line.endswith("═══"):
                parts.append(f'<h3 style="margin:14px 0 4px;color:#4a7db8;">{html_escape(line)}</h3>')
            elif line.startswith("━━━") and line.endswith("━━━"):
                parts.append(f'<h3 style="margin:12px 0 4px;color:#1f3a5f;">{html_escape(line)}</h3>')
            elif line.startswith("⚠"):
                parts.append(f'<div style="color:#b06a00;">{html_escape(line)}</div>')
            else:
                parts.append(f"<div>{html_escape(line)}</div>")
        return (
            '<div style="font-family:Segoe UI,Microsoft YaHei,sans-serif;'
            'font-size:13px;line-height:1.7;color:#2b313a;">' + "".join(parts) + "</div>"
        )

    def _notify_summary(
        self,
        rounds: list[list[tuple[str, RunResult]]],
        elapsed: float,
    ) -> None:
        """全部账号 × 多轮执行完成后发送一封汇总邮件

        包含：总耗时、轮数/账号数/成功率、每轮各账号执行结果（状态/任务/扫荡
        明细）、最终剩余体力。单轮（run_times=1，默认）时保持原有的账号块排版；
        多轮时每轮一个「═══ 第 N 轮 ═══」分节，节内是该轮各账号的详情块，
        一眼即可看出每一轮的结果。
        收件人取全局通知配置（notify.email.to_addrs）。
        开启 notify.attach_game_screenshot 时，每个账号只内联**最后一轮**的
        游戏主界面截图（相邻轮次的画面基本一致，避免邮件塞入 N 倍重复图片）。
        """
        if not self.config.notify.enabled:
            return
        notifier = EmailNotifier(self.config.notify.email)
        if not notifier.enabled:
            return

        flat = [(acc_id, r) for round_results in rounds for acc_id, r in round_results]
        total = len(flat)
        failed_count = sum(1 for _, r in flat if r.status == "failed")
        success_count = total - failed_count
        num_rounds = len(rounds)
        per_round = len(rounds[0]) if rounds else 0
        status_text = (
            f"全部成功（{success_count}/{total}）" if failed_count == 0
            else f"{failed_count}/{total} 次账号执行失败"
        )

        # 总耗时格式化
        minutes, secs = divmod(int(elapsed), 60)
        hours, minutes = divmod(minutes, 60)
        duration_str = f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"

        subject = (
            f"[BAAS-Plus] 执行汇总 — {status_text}"
            + (f"（{num_rounds} 轮，耗时 {duration_str}）" if num_rounds > 1
               else f"（耗时 {duration_str}）")
        )

        # 构建邮件正文
        lines: list[str] = []
        lines.append(f"总耗时: {duration_str}")
        if num_rounds > 1:
            lines.append(
                f"执行轮数: {num_rounds}（每轮 {per_round} 个账号，共 {total} 次账号执行）"
            )
            lines.append(f"成功: {success_count}，失败: {failed_count}")
        else:
            lines.append(f"账号数: {total}，成功: {success_count}，失败: {failed_count}")
        lines.append("")

        for round_no, round_results in enumerate(rounds, start=1):
            if num_rounds > 1:
                lines.append(f"═══ 第 {round_no} 轮 ═══")
            for account_id, result in round_results:
                account = self.get_account(account_id)
                lines.append(f"━━━ [{account.name}] {account.baas.server} ━━━")
                lines.append(f"状态: {result.status}")
                lines.append(f"摘要: {result.summary}")
                if result.executed_tasks:
                    lines.append(f"任务: {', '.join(result.executed_tasks)}")
                if result.swept:
                    lines.append(f"扫荡: {', '.join(result.swept)}")
                if result.warnings:
                    lines.append(f"⚠ 警告:")
                    for w in result.warnings:
                        lines.append(f"  - {w}")
                lines.append("")

        # 最终剩余体力（取最后一轮最后一个成功执行账号的体力）
        last_ap = -1
        for _, result in reversed(flat):
            if result.ap_before_sweep >= 0:
                last_ap = result.ap_before_sweep
                break
        if last_ap >= 0:
            lines.append(f"最终剩余体力: {last_ap}")
        else:
            lines.append("最终剩余体力: 无法读取")

        # 汇总邮件正文：纯文本 + HTML 双形态；开启截图时把每个账号**最后一轮**
        # 执行完成时的游戏主界面截图像内联图片嵌进 HTML（cid 引用）
        images: list[tuple[str, str]] = []
        if self.config.notify.attach_game_screenshot:
            seen: set[str] = set()
            for round_results in reversed(rounds):
                for account_id, result in round_results:
                    if account_id in seen:
                        continue
                    shot = getattr(result, "screenshot_path", "")
                    if shot:
                        images.append((f"ba_{account_id}", shot))
                        seen.add(account_id)

        html = self._to_html(lines)
        if images:
            shot_blocks = ['<h3 style="margin:14px 0 6px;color:#1f3a5f;">📸 执行完成画面（游戏主界面）</h3>']
            for cid, _ in images:
                shot_blocks.append(
                    f'<img src="cid:{cid}" alt="执行完成画面" width="100%" '
                    'style="max-width:560px;border:1px solid #dde1e8;border-radius:8px;margin-bottom:8px;"/>'
                )
            html = html.replace("</div>", "".join(shot_blocks) + "</div>", 1)

        notifier.send_html(subject, html, text="\n".join(lines), images=images)
