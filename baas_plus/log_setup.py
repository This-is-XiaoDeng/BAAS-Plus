"""BAAS-Plus 独立日志配置（文件 + 控制台）

为什么不用 root logger：
BAAS 的 Main() 初始化会重置 root logger 的 handlers（BAAS 自带日志系统），
若把 FileHandler 挂在 root 上，create_baas 之后 baas_plus.log 会停止写入
（现象：日志断在「初始化 BAAS Main」之后，后续流程无任何 BAAS-Plus 日志，
但代码实际在跑——BAAS 侧/控制台能看到 OCR 等行为）。

因此 BAAS-Plus 使用独立命名空间 baas_plus.*，自带 FileHandler + StreamHandler，
propagate=False，与 BAAS 日志系统彻底隔离。任何入口（CLI/WebUI/测试）
import 本模块即完成配置；重复调用幂等（已有 handler 则跳过）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "data"
_FORMAT = "[%(asctime)s][%(name)s / %(levelname)s]: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    """配置（或确认）BAAS-Plus 独立日志；幂等，可多次调用"""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("baas_plus")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    fh = logging.FileHandler(_LOG_DIR / "baas_plus.log", encoding="utf-8")
    fh.setFormatter(formatter)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(sh)


setup_logging()
