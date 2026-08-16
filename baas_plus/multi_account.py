"""多账号批量执行编排

串行逐个账号执行完整流程（模拟器 → 活动检测/推图 → 任务 → 扫荡 → 通知），
账号间完全隔离：每个账号独立的 Engine / BaasBridge / 模拟器实例。
共享进程级资源：
- BAAS Main（OCR 服务器进程）：跨账号复用，避免重复拉起（create_baas 仅在
  _main 为 None 时新建，Runner 注入后取回继续传给下一个账号）；
- ActivityFetcher：按 server 缓存实例（同服活动数据只需拉取一次）。

失败隔离：单个账号异常不外抛，兜底为 failed 的 RunResult，不影响其余账号。
"""
from __future__ import annotations

import logging
from typing import Callable

from .activity import ActivityFetcher
from .baas_bridge import BaasBridge
from .config import AccountConfig, AppConfig
from .engine import Engine, RunResult
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
            notify=self.config.notify,
            data_dir=str(self.config.data_path),
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
        """串行执行全部启用账号，返回 [(account_id, RunResult), ...]"""
        results: list[tuple[str, RunResult]] = []
        for account in self.enabled_accounts():
            results.append(await self.run_account(account.id))
        return results
