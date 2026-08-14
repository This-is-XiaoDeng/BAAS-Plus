# 🐱 BAAS-Plus

基于 [BAAS (Blue Archive Auto Script)](https://github.com/pur1fying/blue_archive_auto_script) 的蔚蓝档案自动化调度器。

把「每天手动打开模拟器 → 推图 → 扫荡 → 查体力」变成：
**Windows 计划任务启动 → 自动开模拟器 → 检测新活动 → 活动推图 → 执行勾选任务 → 按剩余体力自动扫荡 → 邮件通知**

## ✨ 功能

- **自动启动模拟器**：通过 Mumu / 雷电等模拟器进程 API 启动并获取 ADB 端口（复用 BAAS `emulator_manager`）
- **活动检测**：内置 GameKee 数据源抓取活动/卡池/总力战（参考 [BlueArchive.ics](https://github.com/This-is-XiaoDeng/BlueArchive.ics)），本地 SQLite 记录已处理活动
- **新活动自动推图**：检测到未处理过的活动 → 自动执行活动推图（剧情/任务/挑战可分别开关）
- **按体力扫荡**：所有勾选任务完成后读取实时体力：
  - 有活动 → 优先扫活动关卡（BAAS `activity_sweep`，`-1` = 按 AP 自动计算次数）
  - 无活动 → 扫普通/困难图（`auto` 模式按剩余体力重算每关次数，困难图封顶 3 次）
- **邮件通知**：执行完成后推送结果摘要（SMTP，支持 QQ 邮箱授权码）
- **WebUI**：浏览器配置（任务勾选/模拟器/活动/扫荡/邮件）+ 查看活动状态与执行记录 + 手动触发

## 🏗 架构

```
BAAS-Plus（调度器/策略层，AGPL-3.0）
  ├── 不魔改 BAAS：仅通过官方 API（Baas_thread.solve / ConfigSet / emulator_manager）驱动
  ├── baas_plus/engine.py      核心编排：模拟器 → 活动检测 → 推图 → 任务 → 扫荡 → 通知
  ├── baas_plus/activity.py    GameKee 活动数据源（httpx）
  ├── baas_plus/store.py       SQLite：活动状态 + 执行记录（线程安全）
  ├── baas_plus/baas_bridge.py BAAS 集成层（惰性导入，Windows 部署时安装）
  ├── baas_plus/notifier.py    邮件通知（smtplib）
  └── baas_plus/webui/         FastAPI + 单页前端
```

BAAS 作为可选 git 依赖（`poetry install -E baas`），仅在 Windows 运行环境需要。

## 🚀 快速开始（Windows）

```bash
# 1. 安装依赖（含 BAAS 本体）
poetry install -E baas

# 2. 生成默认配置并启动 WebUI
poetry run python -m baas_plus.cli webui
# 打开 http://127.0.0.1:18080 配置：模拟器、勾选任务、扫荡列表、SMTP 邮箱

# 3. 手动试跑一次
poetry run python -m baas_plus.cli run

# 4. 测试邮件
poetry run python -m baas_plus.cli test-email

# 5. 仅查看活动（不执行）
poetry run python -m baas_plus.cli scan
```

### Windows 计划任务

```
schtasks /create /tn "BAAS-Plus-Daily" /tr "<BAAS-Plus 目录>\run.bat" /sc daily /st 05:00
```

`run.bat` 内容示例：

```bat
@echo off
cd /d D:\BAAS-Plus
call poetry run python -m baas_plus.cli run
```

## ⚙️ 配置说明

配置文件 `data/config.json`（WebUI 可视化编辑）：

| 段 | 关键项 | 说明 |
|---|---|---|
| `simulator` | `type` / `instance` | 模拟器类型（mumu/leidian/...）与多开编号 |
| `baas` | `repo_dir` / `config_dir` / `tasks` | BAAS 源码目录（留空用安装包）、BAAS 配置目录名、勾选任务 |
| `baas` | `current_activity` | 手动指定活动模块名（无法自动映射时使用） |
| `activity` | `push_story/mission/challenge_on_new` | 新活动自动推图开关 |
| `sweep` | `strategy` | `auto`（按体力算次数）/ `fixed`（固定次数） |
| `sweep` | `normal_tasks` / `hard_tasks` | 扫荡列表，格式 `region-mission-counts`（counts 可为数字或 `max`） |
| `sweep` | `activity_task_number` | 活动扫荡关卡号（如 `1` 或 `1,2`） |
| `notify.email` | SMTP 配置 | QQ 邮箱用 465 + SSL + 授权码 |

### 勾选任务（BAAS 任务清单）

`cafe_reward` 咖啡厅 · `lesson` 课程表 · `collect_reward` 领取奖励 · `collect_daily_free_power` 每日免费体力 · `group` 社团 · `mail` 邮件 · `friend` 好友 · `main_story` 主线剧情 · `scrimmage` 演习 · `arena` 竞技场 · `joint_firing_drill` 联合作战 · `rewarded_task` 悬赏通缉 · `clear_special_task_power` 特别依赖 · `create` 制造 · `explore_normal_task`/`explore_hard_task` 推图 · `normal_task`/`hard_task` 扫荡 · `activity_sweep` 活动扫荡 · `explore_activity_story/challenge/mission` 活动推图 等

## ⚠️ 已知限制

- **活动模块映射**：GameKee 活动标题（中文）无法自动对应 BAAS 活动模块名（`module/activities/<Name>.py`，英文驼峰）。检测到新活动时若未配置 `baas.current_activity` 或映射，会记录活动并跳过自动推图（WebUI 会提示）
- **活动推图依赖 BAAS 支持**：BAAS 的活动功能是插件化的（每个活动一个模块，由社区维护），不支持的模组会跳过并记日志
- **BAAS 仅 Windows**：调度器可在任意平台开发/测试，但实际运行需要 Windows + 模拟器

## 📄 许可证

[AGPL-3.0](LICENSE)

- BAAS 本体为 GPL-3.0：AGPL §13 明确允许与 GPL-3 作品结合，本项目仅以外部依赖方式调用，未修改其源码
- 活动数据源逻辑参考 [BlueArchive.ics](https://github.com/This-is-XiaoDeng/BlueArchive.ics)（AGPL-3.0，同作者）
