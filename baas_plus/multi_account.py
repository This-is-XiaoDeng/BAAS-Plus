"""多账号批量执行编排

串行逐个账号执行完整流程（模拟器 → 活动检测/推图 → 任务 → 扫荡），
账号间完全隔离：每个账号独立的 Engine / BaasBridge / 模拟器实例。
共享进程级资源：
- BAAS Main（OCR 服务器进程）：跨账号复用，避免重复拉起（create_baas 仅在
  _main 为 None 时新建，Runner 注入后取回继续传给下一个账号）；
- ActivityFetcher：按 server 缓存实例（同服活动数据只需拉取一次）。

失败隔离：单个账号异常不外抛，兜底为 failed 的 RunResult，不影响其余账号。
全部账号执行完成后统一发送一封汇总邮件（总耗时 + 各账号结果 + 最终剩余体力）。
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

    async def run_all(self) -> list[tuple[str, RunResult]]:
        """串行执行全部启用账号，统一发送汇总邮件，返回 [(account_id, RunResult), ...]"""
        start = time.monotonic()
        results: list[tuple[str, RunResult]] = []
        for account in self.enabled_accounts():
            results.append(await self.run_account(account.id))
        elapsed = time.monotonic() - start
        self._notify_summary(results, elapsed)
        return results

    def _to_html(self, lines: list[str]) -> str:
        """把纯文本汇总行转成简单的 HTML 邮件正文（账号标题行加粗着色）"""
        parts: list[str] = []
        for line in lines:
            if not line.strip():
                parts.append('<div style="height:6px;"></div>')
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
        results: list[tuple[str, RunResult]],
        elapsed: float,
    ) -> None:
        """全部账号执行完成后发送一封汇总邮件

        包含：总耗时、各账号执行结果（状态/任务/扫荡明细）、最终剩余体力。
        收件人取全局通知配置（notify.email.to_addrs）。
        开启 notify.attach_game_screenshot 时，把 Engine 执行结束时截取的游戏
        主界面画面（每个账号一张）作为内联图片嵌入邮件。
        """
        if not self.config.notify.enabled:
            return
        notifier = EmailNotifier(self.config.notify.email)
        if not notifier.enabled:
            return

        total = len(results)
        failed_count = sum(1 for _, r in results if r.status == "failed")
        success_count = total - failed_count
        status_text = (
            f"全部成功（{success_count}/{total}）" if failed_count == 0
            else f"{failed_count}/{total} 个账号失败"
        )

        # 总耗时格式化
        minutes, secs = divmod(int(elapsed), 60)
        hours, minutes = divmod(minutes, 60)
        duration_str = f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"

        subject = f"[BAAS-Plus] 执行汇总 — {status_text}（耗时 {duration_str}）"

        # 构建邮件正文
        lines: list[str] = []
        lines.append(f"总耗时: {duration_str}")
        lines.append(f"账号数: {total}，成功: {success_count}，失败: {failed_count}")
        lines.append("")

        for account_id, result in results:
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

        # 最终剩余体力（取最后一个成功执行的账号的体力）
        last_ap = -1
        for _, result in reversed(results):
            if result.ap_before_sweep >= 0:
                last_ap = result.ap_before_sweep
                break
        if last_ap >= 0:
            lines.append(f"最终剩余体力: {last_ap}")
        else:
            lines.append("最终剩余体力: 无法读取")

        # 汇总邮件正文：纯文本 + HTML 双形态；开启截图时把各账号执行完成时的
        # 游戏主界面截图像内联图片嵌进 HTML（cid 引用），收件人无需打开模拟器
        images: list[tuple[str, str]] = []
        if self.config.notify.attach_game_screenshot:
            for account_id, result in results:
                if getattr(result, "screenshot_path", ""):
                    images.append((f"ba_{account_id}", result.screenshot_path))

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
