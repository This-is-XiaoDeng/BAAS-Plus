"""命令行入口（Windows 计划任务 / 手动执行均通过此入口）

用法：
    python -m baas_plus.cli run            # 执行一次完整流程
    python -m baas_plus.cli webui          # 启动 WebUI（配置 + 执行记录）
    python -m baas_plus.cli scan           # 仅活动检测（打印新活动，不执行）
    python -m baas_plus.cli test-email     # 发送测试邮件
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .activity import ActivityFetcher
from .config import load_config
from .engine import Engine
from .notifier import EmailNotifier
from .store import Store

# 日志目录（data/ 被 .gitignore 排除，clone 后不存在，必须先创建）
_LOG_DIR = Path(__file__).resolve().parent.parent / "data"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(name)s / %(levelname)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_DIR / "baas_plus.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("baas_plus.cli")


def cmd_run(config_path: str | None) -> int:
    config = load_config(config_path)
    logger.info("BAAS-Plus %s 开始执行（server=%s, 任务=%s）", __version__, config.baas.server, config.baas.tasks)
    engine = Engine(config)
    try:
        result = asyncio.run(engine.run_once())
    except Exception as exc:  # noqa: BLE001
        logger.exception("执行异常")
        return 1
    print("\n===== 执行结果 =====")
    print(result.summary)
    for task in result.executed_tasks:
        print(f"  ✓ {task}")
    for swept in result.swept:
        print(f"  ⚡ 扫荡 {swept}")
    return 0 if result.status != "failed" else 1


def cmd_scan(config_path: str | None) -> int:
    config = load_config(config_path)
    store = Store(config.data_path / "baas_plus.db")
    fetcher = ActivityFetcher(config.activity.server)

    async def _scan() -> int:
        events = await fetcher.fetch_all()
        new_events = [e for e in events if not store.is_activity_seen(e)]
        print(f"当前活动事件 {len(events)} 个，新事件 {len(new_events)} 个：")
        for event in events:
            mark = "🆕" if event in new_events else "  "
            print(f"  {mark} [{event.event_type.value}] {event.title} (id={event.id})")
        for event in new_events:
            store.mark_activity_seen(event)
        return 0

    return asyncio.run(_scan())


def cmd_webui(config_path: str | None) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .webui.app import create_app

    config = load_config(config_path)
    app = create_app(config)
    url = f"http://127.0.0.1:{config.webui.port}"
    print(f"WebUI: {url}")
    # 延迟 1 秒等 uvicorn 起来后自动打开浏览器（0.0.0.0 绑定时用 127.0.0.1 访问）
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.webui.host, port=config.webui.port)
    return 0


def cmd_test_email(config_path: str | None) -> int:
    config = load_config(config_path)
    ok = EmailNotifier(config.notify.email).send("BAAS-Plus 测试邮件", "这是一封测试邮件，收到即表示配置正确。")
    print("测试邮件发送", "成功" if ok else "失败（请检查 SMTP 配置）")
    return 0 if ok else 1


def cmd_test_ocr(config_path: str | None) -> int:
    """调试：启动模拟器/BAAS 后对主页轮播图区域做一次 OCR，打印原始识别文本

    用于排查"轮播图 OCR 识别不出东西"：
    - 空输出 + 日志无 OCR 行 → 预处理/OCR 服务链路问题
    - 有输出但都是 l/1/o/0 等非中文字符 → 艺术字识别偏差（匹配层会兜底）
    """
    import time

    config = load_config(config_path)
    from .baas_bridge import BaasBridge

    logger.info("启动模拟器（用于取截图）...")
    bridge = BaasBridge(config)
    adb = bridge.start_simulator()
    bridge.create_baas(adb)
    # 等模拟器就绪 + 游戏画面稳定（若游戏未启动，先启动到主页）
    bridge.launch_game()
    for i in range(1, 7):
        time.sleep(2)
        text = bridge.ocr_banner()
        print(f"\n=== 第 {i} 次 OCR（每 2s 一次，轮播图自动换页时文本会变）===")
        print(f"原始识别文本: {text!r}")
        if not text:
            print("(空输出：OCR 没吐任何字符)")
    bridge.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baas-plus", description="基于 BAAS 的蔚蓝档案自动化调度器")
    parser.add_argument("command", choices=["run", "scan", "webui", "test-email", "test-ocr"])
    parser.add_argument("--config", default=None, help="配置文件路径（默认 data/config.json）")
    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(args.config)
    if args.command == "scan":
        return cmd_scan(args.config)
    if args.command == "webui":
        return cmd_webui(args.config)
    if args.command == "test-email":
        return cmd_test_email(args.config)
    if args.command == "test-ocr":
        return cmd_test_ocr(args.config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
