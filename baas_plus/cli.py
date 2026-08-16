"""命令行入口（Windows 计划任务 / 手动执行均通过此入口）

用法：
    python -m baas_plus.cli run                      # 执行全部启用账号
    python -m baas_plus.cli run --account <id|name>  # 只执行指定账号
    python -m baas_plus.cli webui                    # 启动 WebUI（配置 + 执行记录）
    python -m baas_plus.cli scan                     # 仅活动检测（打印新活动，不执行）
    python -m baas_plus.cli test-email               # 发送测试邮件
    python -m baas_plus.cli test-ocr                 # 调试：打印主页轮播图 OCR 原始文本
    python -m baas_plus.cli reset-push               # 重置已推送/已推图的活动标记
    python -m baas_plus.cli reset-push --key <key>   # 只重置指定活动
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .activity import ActivityFetcher
from .config import load_config
from .log_setup import setup_logging
from .multi_account import MultiAccountRunner
from .notifier import EmailNotifier

# BAAS-Plus 独立日志（文件 data/baas_plus.log + 控制台）：
# 用独立命名空间 baas_plus.*，避免 BAAS 的 Main() 初始化重置 root logger
# 的 handlers 导致后续日志全部丢失（现象：日志断在「初始化 BAAS Main」之后）
setup_logging()
logger = logging.getLogger("baas_plus.cli")


def _target_accounts(runner: MultiAccountRunner, account: str | None) -> list:
    """解析 --account 参数：指定时返回单个账号，缺省返回全部启用账号"""
    if account:
        return [runner.get_account(account)]
    return runner.enabled_accounts()


def cmd_run(config_path: str | None, account: str | None = None) -> int:
    config = load_config(config_path)
    runner = MultiAccountRunner(config)
    logger.info("BAAS-Plus %s 开始执行（账号数=%d）", __version__, len(runner.enabled_accounts()))

    async def _run() -> list:
        if account:
            return [await runner.run_account(account)]
        return await runner.run_all()

    try:
        results = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("执行异常")
        return 1

    all_ok = True
    for account_id, result in results:
        name = runner.get_account(account_id).name
        print(f"\n===== 账号 [{name}] 执行结果: {result.status} =====")
        print(result.summary)
        for task in result.executed_tasks:
            print(f"  ✓ {task}")
        for swept in result.swept:
            print(f"  ⚡ 扫荡 {swept}")
        if result.status == "failed":
            all_ok = False
    return 0 if all_ok else 1


def cmd_scan(config_path: str | None, account: str | None = None) -> int:
    config = load_config(config_path)
    runner = MultiAccountRunner(config)
    targets = _target_accounts(runner, account)

    async def _scan() -> int:
        for acc in targets:
            fetcher = ActivityFetcher(acc.activity.server)
            events = await fetcher.fetch_all()
            new_events = [e for e in events if not runner.store.is_activity_seen(acc.id, e)]
            print(f"\n===== 账号 [{acc.name}] 当前活动事件 {len(events)} 个，新事件 {len(new_events)} 个：=====")
            for event in events:
                mark = "🆕" if event in new_events else "  "
                print(f"  {mark} [{event.event_type.value}] {event.title} (id={event.id})")
            for event in new_events:
                runner.store.mark_activity_seen(acc.id, event)
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
    bridge = BaasBridge(config)  # 默认账号（config 兼容属性代理到 accounts[0]）
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


def cmd_reset_push(
    config_path: str | None, activity_key: str | None = None, account: str | None = None
) -> int:
    """重置已推送/已推图的活动标记（让活动重新触发推图）

    用法：
        python -m baas_plus.cli reset-push                    # 重置全部账号
        python -m baas_plus.cli reset-push --account <id|name>  # 只重置指定账号
        python -m baas_plus.cli reset-push --key <key>        # 只重置指定活动
    """
    config = load_config(config_path)
    runner = MultiAccountRunner(config)
    targets = _target_accounts(runner, account)
    total = 0
    for acc in targets:
        rows = runner.store.list_activities(acc.id, limit=500)
        pushed = [r for r in rows if r["pushed"]]
        print(f"\n账号 [{acc.name}]：共 {len(pushed)} 条已推送记录")
        for r in pushed:
            mark = " <- 本次重置" if activity_key and r["event_key"] == activity_key else ""
            print(f"  {r['event_key']}  {r['title']}{mark}")
        total += runner.store.reset_pushed(acc.id, activity_key)
    if total == 0:
        print("没有已推送/已推图的活动记录")
        return 0
    print(f"已重置 {total} 条活动推送标记，下次执行将重新触发推图")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baas-plus", description="基于 BAAS 的蔚蓝档案自动化调度器")
    parser.add_argument("command", choices=["run", "scan", "webui", "test-email", "test-ocr", "reset-push"])
    parser.add_argument("--config", default=None, help="配置文件路径（默认 data/config.json）")
    parser.add_argument("--account", default=None, help="指定账号（id 或名称）；缺省执行全部启用账号")
    parser.add_argument("--key", default=None, help="reset-push: 只重置指定活动 key")
    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(args.config, args.account)
    if args.command == "scan":
        return cmd_scan(args.config, args.account)
    if args.command == "webui":
        return cmd_webui(args.config)
    if args.command == "test-email":
        return cmd_test_email(args.config)
    if args.command == "test-ocr":
        return cmd_test_ocr(args.config)
    if args.command == "reset-push":
        return cmd_reset_push(args.config, args.key, args.account)
    return 1


if __name__ == "__main__":
    sys.exit(main())
