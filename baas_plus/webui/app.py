"""WebUI：FastAPI 应用

功能：
- GET  /                     前端页面（静态）
- GET  /api/config           读取配置（含账号列表）
- PUT  /api/config           保存配置（含任务勾选、扫荡策略、邮件设置）
- POST /api/accounts         新建账号（复制默认账号配置，仅改 id/name）
- DELETE /api/accounts/{id}  删除账号（至少保留一个；历史记录保留不自动清理）
- GET  /api/records          执行记录（?account=<id> 过滤）
- GET  /api/activities       活动状态（?account=<id>，默认第一个账号）
- POST /api/scan             手动刷新活动检测（?account=<id>）
- POST /api/run              手动触发执行（body.account=<id> 或 "all"）
- POST /api/test-email       发送测试邮件
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..activity import ActivityFetcher
from ..config import (
    AccountConfig,
    AppConfig,
    BAAS_TASKS,
    SWEEP_TASKS,
    TASK_LABELS,
    _migrate_legacy_config,
    save_config,
)
from ..engine import Engine
from ..multi_account import MultiAccountRunner
from ..notifier import EmailNotifier
from ..store import Store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="BAAS-Plus WebUI", version="0.1.0")
    store = Store(config.data_path / "baas_plus.db")

    def resolve_account(ref: Optional[str]) -> AccountConfig:
        """按账号 id 解析（缺省 = 第一个账号）；找不到抛 404"""
        if ref:
            for acc in config.accounts:
                if acc.id == ref:
                    return acc
            raise HTTPException(status_code=404, detail=f"账号不存在: {ref}")
        return config.accounts[0]

    # ---- 配置 ----

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return config.model_dump()

    @app.put("/api/config")
    def put_config(body: dict[str, Any]) -> dict[str, Any]:
        nonlocal config
        try:
            config = AppConfig.model_validate(_migrate_legacy_config(body))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"配置校验失败: {exc}") from exc
        save_config(config)
        # 「模拟器&BAAS」设置更新后：从 BAAS 配置同步扫荡列表（普通/困难图为空时填充）
        # 并应用 BA 游戏包名；BAAS 不可用时不影响保存。
        # 多账号下 BAAS 共享 config 目录，同步只针对默认账号（accounts[0]）。
        sync = None
        default_account = config.accounts[0]
        if default_account.baas.repo_dir:
            try:
                from ..baas_bridge import BaasBridge

                bridge = BaasBridge(default_account)
                sync = bridge.sync_sweep_from_baas()
                if sync.get("applied"):
                    save_config(config)
            except Exception as exc:  # noqa: BLE001
                sync = {"ok": False, "reason": str(exc)}
        return {"ok": True, "sync": sync}

    @app.get("/api/tasks")
    def get_tasks() -> list[dict[str, str]]:
        """可勾选任务列表（含中文名）；扫荡类任务由扫荡阶段统一调度，不在列表中显示"""
        return [
            {"name": t, "label": TASK_LABELS.get(t, t)}
            for t in BAAS_TASKS
            if t not in SWEEP_TASKS
        ]

    @app.get("/api/baas-config-dirs")
    def get_baas_config_dirs(account: Optional[str] = None) -> list[str]:
        """BAAS 配置目录候选（BAAS 根 config/ 下的子目录；读取失败时返回内置选项）"""
        import os

        builtin = ["cn", "global", "jp", "steam"]
        acc = resolve_account(account)
        repo_dir = acc.baas.repo_dir
        if repo_dir:
            base = Path(repo_dir) / "config"
        else:
            base = Path.cwd() / "config"
        try:
            if base.is_dir():
                dirs = sorted(d.name for d in base.iterdir() if d.is_dir())
                if dirs:
                    return dirs
        except OSError:
            pass
        return builtin

    # ---- 账号管理 ----

    @app.post("/api/accounts")
    def create_account(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """新建账号：复制默认账号（accounts[0]）配置，仅替换 id/name"""
        nonlocal config
        template = config.accounts[0]
        new = template.model_copy(deep=True)
        new.id = f"acc_{uuid.uuid4().hex[:8]}"
        new.name = ((body or {}).get("name") or "").strip() or f"账号 {len(config.accounts) + 1}"
        new.enabled = True
        config.accounts.append(new)
        save_config(config)
        return new.model_dump()

    @app.delete("/api/accounts/{account_id}")
    def delete_account(account_id: str) -> dict[str, Any]:
        """删除账号（至少保留一个；历史记录保留在 SQLite 中，不自动清理）"""
        nonlocal config
        if len(config.accounts) <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个账号")
        before = len(config.accounts)
        config.accounts = [a for a in config.accounts if a.id != account_id]
        if len(config.accounts) == before:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")
        save_config(config)
        return {"ok": True}

    # ---- 执行记录 ----

    @app.get("/api/records")
    def get_records(limit: int = 50, account: Optional[str] = None) -> list[dict]:
        return store.list_records(account=account, limit=limit)

    # ---- 活动 ----

    @app.get("/api/activities")
    async def get_activities(account: Optional[str] = None) -> dict[str, Any]:
        acc = resolve_account(account)
        fetcher = ActivityFetcher(acc.activity.server)
        try:
            current = await fetcher.fetch_all()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"活动数据源拉取失败: {exc}") from exc
        seen = {r["event_key"] for r in store.list_activities(acc.id, limit=500)}
        sweepable = [e for e in current if e.is_sweepable]  # 仅常规活动（总力战/大决战/卡池不参与扫荡）
        other = [e for e in current if not e.is_sweepable]
        return {
            "account": acc.id,
            "account_name": acc.name,
            "current": [e.__dict__ for e in sweepable],
            "other": [e.__dict__ for e in other],
            "seen_keys": sorted(seen),
            "new": [e.__dict__ for e in current if e.key not in seen],
        }

    @app.post("/api/scan")
    async def scan(account: Optional[str] = None) -> dict[str, Any]:
        acc = resolve_account(account)
        engine = Engine(
            acc,
            account_id=acc.id,
            store=store,
            fetcher=ActivityFetcher(acc.activity.server),
        )
        new_events = await engine.detect_new_activities()
        return {
            "account": acc.id,
            "new_count": len(new_events),
            "new": [e.__dict__ for e in new_events],
        }

    # ---- 执行 ----

    class RunBody(BaseModel):
        account: Optional[str] = None  # 账号 id；缺省或 "all" = 全部启用账号

    @app.post("/api/run")
    async def run(body: Optional[RunBody] = None) -> dict[str, Any]:
        runner = MultiAccountRunner(config, store=store)
        ref = (body.account if body else None) or "all"
        if ref == "all":
            enabled = runner.enabled_accounts()
            if not enabled:
                raise HTTPException(status_code=400, detail="没有启用状态的账号")
            # 多账号串行：每账号 600s 上限，总超时按账号数放大
            timeout = 600 * max(1, len(enabled))
            try:
                results = await asyncio.wait_for(runner.run_all(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error("批量执行超时（>%ss）", timeout)
                raise HTTPException(
                    status_code=408,
                    detail=f"批量执行超时（>{timeout}s）：多账号串行总时长超限，请查看 data/baas_plus.log 定位卡点",
                ) from None
            failed = [r for _, r in results if r.status == "failed"]
            status = "failed" if failed else (
                "success" if all(r.status == "success" for _, r in results) else "partial"
            )
            summary = "；".join(
                f"[{runner.get_account(aid).name}] {r.summary}" for aid, r in results
            )
            return {
                "status": status,
                "summary": summary,
                "results": [
                    {"account": aid, "status": r.status, "summary": r.summary}
                    for aid, r in results
                ],
            }
        acc = resolve_account(ref)
        try:
            # 600s 超时：BAAS 初始化（OCR 服务器/设备连接）卡住时返回明确错误，
            # 而不是让请求无限挂起；超时后 run_once 协程仍在后台，但不阻塞 UI
            _, result = await asyncio.wait_for(runner.run_account(acc.id), timeout=600)
        except asyncio.TimeoutError:
            logger.error("执行超时（>600s）：大概率卡在 BAAS 初始化（OCR 服务器/设备连接）")
            raise HTTPException(
                status_code=408,
                detail="执行超时：大概率卡在 BAAS 初始化（OCR 服务器/设备连接），请查看 data/baas_plus.log 定位卡点",
            ) from None
        return {
            "account": acc.id,
            "status": result.status,
            "summary": result.summary,
            "executed_tasks": result.executed_tasks,
            "swept": result.swept,
            "new_activities": [e.__dict__ for e in result.new_activities],
        }

    # ---- 邮件 ----

    @app.post("/api/test-email")
    def test_email() -> dict[str, Any]:
        notifier = EmailNotifier(config.notify.email)
        ok = notifier.send(
            "BAAS-Plus 测试邮件", "这是一封测试邮件，收到即表示 SMTP 配置正确。"
        )
        return {"ok": ok, "error": notifier.last_error}

    # ---- 测试 - 模拟器 / BAAS ----

    @app.post("/api/test-simulator")
    def test_simulator(account: Optional[str] = None) -> dict[str, Any]:
        from ..baas_bridge import BaasBridge

        acc = resolve_account(account)
        bridge = BaasBridge(acc)
        try:
            adb = bridge.start_simulator()
            return {"ok": True, "adb": adb, "message": f"模拟器已启动，ADB 地址: {adb}"}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"模拟器测试失败: {exc}") from exc

    @app.post("/api/test-baas")
    def test_baas(account: Optional[str] = None) -> dict[str, Any]:
        from ..baas_bridge import BaasBridge

        acc = resolve_account(account)
        bridge = BaasBridge(acc)
        try:
            info = bridge.check_baas()
            return {"ok": True, **info}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"BAAS 测试失败: {exc}") from exc

    # ---- 静态页面 ----

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(Exception)
    async def unhandled(_: Any, exc: Exception) -> JSONResponse:
        logger.exception("WebUI 未处理异常")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app
