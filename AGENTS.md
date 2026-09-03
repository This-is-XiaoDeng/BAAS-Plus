# AGENTS.md

面向 AI 编码代理的项目指南（也适合人类开发者快速上手）。项目文档与注释均为中文，请保持一致。

## 项目简介

BAAS-Plus 是《蔚蓝档案》自动化调度器：自动启动模拟器 → 检测新活动并推图 → 执行日常任务 → 按剩余体力扫荡 → 邮件通知 → 自动关模拟器。它**不修改 BAAS 源码**，仅通过调用 BAAS 提供的接口驱动（基于 [BAAS](https://github.com/pur1fying/blue_archive_auto_script)）。

- 许可证：AGPL-3.0（注意与 BAAS 的 GPL-3.0 结合方式，见 README 许可证一节）
- 仓库：https://github.com/This-is-XiaoDeng/BAAS-Plus

## 技术栈

- Python >= 3.9（无 3.9 专属语法；CI 矩阵跑 3.10 / 3.12）
- 运行依赖：fastapi、uvicorn、httpx、pydantic v2
- 打包：poetry（pyproject.toml）；CI 实际用 `pip install -e .` 安装
- 测试：pytest + pytest-asyncio（`asyncio_mode = "auto"`，async 测试直接写 `async def test_...`）
- **BAAS（blue_archive_auto_script）是可选依赖，本仓库代码绝不直接 import core**（详见「硬性约束」）

## 目录结构

```
baas_plus/
├── cli.py           # 命令行入口（run / webui / scan / test-email / test-ocr / reset-push）
├── config.py        # pydantic v2 配置模型 + JSON 读写（默认 data/config.json，可用环境变量 BAAS_PLUS_CONFIG 覆盖）
├── log_setup.py     # baas_plus.* 独立日志（文件 + 控制台，幂等）
├── engine.py        # 核心编排：模拟器 → 活动检测 → 推图 → 日常任务 → 按体力扫荡 → 通知
├── activity.py      # GameKee 活动数据源（httpx，参考 BlueArchive.ics）
├── baas_bridge.py   # BAAS 集成层（唯一允许接触 core 的模块，全部惰性导入）
├── store.py         # SQLite 状态存储（活动去重 + 执行记录，线程安全）
├── notifier.py      # 邮件通知（smtplib 标准库；支持 HTML + 内联游戏截图）
└── webui/           # FastAPI 应用 + 单页前端（static/，默认 127.0.0.1:18080）
tests/               # pytest 测试（全部 mock，不依赖 BAAS / 模拟器 / 网络 / Windows）
data/                # 运行时数据（gitignored，含 SMTP 授权码等敏感信息）
```

## 常用命令

```bash
pip install -e .            # 安装（poetry install 亦可）
pytest -q                   # 跑全部测试（无需 BAAS / Windows / 网络）
pytest tests/test_engine.py -q

# 模板匹配相关测试依赖 cv2：CI 单独装 opencv-python-headless（不进 pyproject，
# 避免与 BAAS 运行环境的 opencv-python 冲突）。本地若报 ImportError: cv2，自行安装 headless 版。

python -m baas_plus.cli run          # 完整执行（需要 Windows + 模拟器 + BAAS 环境）
python -m baas_plus.cli webui        # 启动 WebUI
python -m baas_plus.cli scan         # 仅活动检测（打印新活动，不执行）
python -m baas_plus.cli test-email   # 发送测试邮件
```

## 硬性约束（改代码前必读）

1. **绝不在模块顶层 import BAAS（core）**。BAAS-Plus 必须能脱离 BAAS 独立安装、测试、运行 WebUI。只有 `baas_bridge.py` 在函数内部惰性导入 core，BAAS 缺失时抛出带指引的 RuntimeError。任何与 BAAS 交互的新代码都要放在 baas_bridge.py，或通过它提供的接口。
2. **日志只用 `baas_plus.*` 命名空间，绝不碰 root logger**。BAAS 的 `Main()` 会重置 root logger 的 handlers，把 FileHandler 挂到 root 上会导致日志静默丢失（现象见 `log_setup.py` 注释）。新模块统一 `logger = logging.getLogger(__name__)`。
3. **注释与文档字符串用中文**，与全库一致。
4. **配置改动必须向后兼容**：`data/config.json` 是用户已有文件。新增字段必须带默认值（pydantic `Field(default_factory=...)`），不要在 `model_validator` 里拒绝合法的旧配置。
5. **data/ 目录是 gitignored 的用户数据**：config.json 含 SMTP 授权码等敏感信息；目录里还有大量嵌套的 `data/data_backup/...` 历史备份。不要读取、提交、清理或移动它们。
6. **扫荡列表格式**：`区域-关卡-次数`（如 `15-1-3`、`20-1-max`，次数可为数字或 `max`）。解析历史配置时可能遇到嵌套转义等脏数据，必须沿用 `baas_bridge._parse_sweep_list` 的正则提取方式，不要改成 `split(',')` 之类的朴素解析。
7. **WebUI 是 FastAPI + 静态单页**：端点逻辑在 `baas_plus/webui/app.py`，前端在 `baas_plus/webui/static/`。`Store` 会被 FastAPI 同步端点在线程池并发调用，保持其线程安全设计（内部 RLock），不要移除锁。
8. **类型与风格**：每个模块顶部 `from __future__ import annotations`；尽量写类型标注；pydantic 一律用 v2 语法（`model_validator` / `model_validate_json`，而非 v1 的 validator / parse_raw）。
9. **版本号两处需同步**：发布/升级版本时同时改 `pyproject.toml` 的 `version` 与 `baas_plus/__init__.py` 的 `__version__`（当前二者不一致：pyproject 为 1.0.0、__init__ 为 0.1.0；如无发布需求不要顺手改动，改版本号时一并对齐即可）。

## 测试约定

- 测试文件放 `tests/`，用 pytest；`asyncio_mode = "auto"` 下无需手动管理 event loop。
- **测试绝不依赖真实 BAAS / 模拟器 / 网络 / Windows**：用 `FakeBridge`、`FakeBaasThread` 等假对象替换 `BaasBridge`（参考 `tests/test_engine.py`、`tests/test_baas_bridge.py`），网络请求用 mock。
- 新增功能请配套测试，重点覆盖：engine 编排顺序（活动推图、扫荡前推图、奖励最后领取等）、baas_bridge 与 BAAS 的交互顺序（曾有「create_baas 必须先 set_ocr 再 init_all_data，否则 OCR pass_method 为 None」的回归坑）。

## 常见任务

- **新增可勾选的日常任务**：在 `config.py` 的 `BAAS_TASKS` / `TASK_LABELS` 各加一行 → 确认 `engine.py` 任务执行路径 → WebUI 前端任务列表（`webui/static/`）→ 补测试。
- **新增 CLI 子命令**：`cli.py` 的 `main()` 加 argparse 分支 + `cmd_xxx()` 函数，并更新文件顶部 docstring 的用法说明。
- **新增活动数据源**：仿照 `activity.py` 的 `ActivityFetcher`（`fetch_all()` 返回 `GameEvent` 列表）；目前 `data_source` 仅支持 `"gamekee"`。
- **改 WebUI 前端**：静态文件在 `baas_plus/webui/static/`（`STATIC_DIR` 见 `webui/app.py`）。
