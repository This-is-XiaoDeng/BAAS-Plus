"""WebUI：FastAPI 应用

功能：
- GET  /                     前端页面（静态）
- GET  /api/config           读取配置
- PUT  /api/config           保存配置（含任务勾选、扫荡策略、邮件设置）
- GET  /api/records          执行记录
- GET  /api/activities       活动状态（本地已见 vs 当前 GameKee 活动）
- POST /api/scan             手动刷新活动检测
- POST /api/run              手动触发一次执行
- POST /api/test-email       发送测试邮件
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..activity import ActivityFetcher
from ..config import AppConfig, BAAS_TASKS, load_config, save_config
from ..engine import Engine
from ..notifier import EmailNotifier
from ..store import Store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="BAAS-Plus WebUI", version="0.1.0")
    store = Store(config.data_path / "baas_plus.db")

    # ---- 配置 ----

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return config.model_dump()

    @app.put("/api/config")
    def put_config(body: dict[str, Any]) -> dict[str, Any]:
        nonlocal config
        try:
            config = AppConfig.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"配置校验失败: {exc}") from exc
        save_config(config)
        return {"ok": True}

    @app.get("/api/tasks")
    def get_tasks() -> list[str]:
        return BAAS_TASKS

    # ---- 执行记录 ----

    @app.get("/api/records")
    def get_records(limit: int = 50) -> list[dict]:
        return store.list_records(limit=limit)

    # ---- 活动 ----

    @app.get("/api/activities")
    async def get_activities() -> dict[str, Any]:
        fetcher = ActivityFetcher(config.activity.server)
        try:
            current = await fetcher.fetch_all()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"活动数据源拉取失败: {exc}") from exc
        seen = {r["event_key"] for r in store.list_activities(limit=500)}
        sweepable = [e for e in current if e.is_sweepable]  # 仅常规活动（总力战/大决战/卡池不参与扫荡）
        other = [e for e in current if not e.is_sweepable]
        return {
            "current": [e.__dict__ for e in sweepable],
            "other": [e.__dict__ for e in other],
            "seen_keys": sorted(seen),
            "new": [e.__dict__ for e in current if e.key not in seen],
        }

    @app.post("/api/scan")
    async def scan() -> dict[str, Any]:
        engine = Engine(config, store=store)
        new_events = await engine.detect_new_activities()
        return {
            "new_count": len(new_events),
            "new": [e.__dict__ for e in new_events],
        }

    # ---- 执行 ----

    class RunBody(BaseModel):
        pass

    @app.post("/api/run")
    async def run(_: RunBody | None = None) -> dict[str, Any]:
        engine = Engine(config, store=store)
        result = await engine.run_once()
        return {
            "status": result.status,
            "summary": result.summary,
            "executed_tasks": result.executed_tasks,
            "swept": result.swept,
            "new_activities": [e.__dict__ for e in result.new_activities],
        }

    # ---- 邮件 ----

    @app.post("/api/test-email")
    def test_email() -> dict[str, Any]:
        ok = EmailNotifier(config.notify.email).send(
            "BAAS-Plus 测试邮件", "这是一封测试邮件，收到即表示 SMTP 配置正确。"
        )
        return {"ok": ok}

    # ---- 测试 - 模拟器 / BAAS ----

    @app.post("/api/test-simulator")
    def test_simulator() -> dict[str, Any]:
        from ..baas_bridge import BaasBridge

        bridge = BaasBridge(config)
        try:
            adb = bridge.start_simulator()
            return {"ok": True, "adb": adb, "message": f"模拟器已启动，ADB 地址: {adb}"}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"模拟器测试失败: {exc}") from exc

    @app.post("/api/test-baas")
    def test_baas() -> dict[str, Any]:
        from ..baas_bridge import BaasBridge

        bridge = BaasBridge(config)
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
