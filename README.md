# 🐱 BAAS-Plus

> 蔚蓝档案自动化调度器：自动开模拟器 → 检测新活动推图 → 执行日常任务 → 按剩余体力扫荡 → 邮件通知

基于 [BAAS](https://github.com/pur1fying/blue_archive_auto_script) 构建，**不修改其源码**，仅通过官方 API 驱动。

## ✨ 特性

| | |
|---|---|
| 🤖 自动启动模拟器 | MuMu / 雷电等，自动获取 ADB 连接 |
| 🎉 新活动自动推图 | GameKee 数据源检测新活动，剧情/任务/挑战可分别开关 |
| ⚡ 按体力扫荡 | 任务完成后读取实时 AP 计算次数，有活动优先扫活动关卡 |
| 📧 邮件通知 | 执行结果推送到邮箱（SMTP，支持 QQ 授权码） |
| 🖥 独立 WebUI | 浏览器配置任务/扫荡/通知，查看活动状态与执行记录 |

## 🚀 快速开始（Windows）

前置：Windows + 模拟器 + 可运行的 [BAAS](https://github.com/pur1fying/blue_archive_auto_script) 环境
（BAAS 源码已装好依赖，能跑官方 GUI 即可；`config/` 目录需存在，release 包自带）

```bash
# 1) 拉取 BAAS-Plus（仅项目自身，不内置 BAAS）
git clone https://github.com/This-is-XiaoDeng/BAAS-Plus.git

# 2) 把 BAAS-Plus 装进 BAAS 的运行环境（editable，与 cli.example.py 同款用法）
cd D:\BAAS                    # BAAS 源码根目录（Python 3.12 + 依赖已就绪）
pip install -e D:\BAAS-Plus

# 3) 从 BAAS 根目录启动 WebUI（import core 与 config/ 相对路径都可用）
python -m baas_plus.cli webui
```

打开 <http://127.0.0.1:18080> 完成配置后：

```bash
python -m baas_plus.cli run         # 立即执行一次完整流程
python -m baas_plus.cli test-email  # 测试邮件通知
```

> 也支持不安装进 BAAS 环境：在 WebUI 配置 `baas.repo_dir` 指向 BAAS 源码目录，
> 引擎会自动把它加入 sys.path 并切换工作目录（两种方式等价）。

### 定时执行（Windows 计划任务）

创建 `run.bat`：

```bat
@echo off
cd /d D:\BAAS
python -m baas_plus.cli run
```

注册每天 05:00 自动执行：

```
schtasks /create /tn "BAAS-Plus-Daily" /tr "D:\BAAS\run.bat" /sc daily /st 05:00
```

## 🖥 WebUI 配置

配置保存在 `data/config.json`，WebUI 中可视化编辑：

- **模拟器**：类型（mumu/雷电/蓝叠等）与多开编号
- **任务**：勾选要执行的日常任务（咖啡厅、课程表、邮件、竞技场等）
- **活动**：新活动自动推图开关，活动数据源选择
- **扫荡**：策略（`auto` 按体力算次数 / `fixed` 固定次数）、扫荡列表、活动关卡号
- **通知**：SMTP 服务器、邮箱账号、授权码、收件人

### 扫荡列表格式

`区域-关卡-次数`，逗号分隔；次数支持数字或 `max`：

```
普通图: 15-1-3, 16-3-5
困难图: 20-1-max      # max = 3 次（困难图单关上限）
```

有活动时自动优先扫活动关卡（活动关卡号在配置中指定）。

## 📦 项目结构

```
baas_plus/
├── engine.py        # 核心编排：模拟器 → 活动 → 任务 → 扫荡 → 通知
├── activity.py      # GameKee 活动数据源
├── baas_bridge.py   # BAAS 集成层（惰性导入，仅 Windows 运行时装 BAAS）
├── store.py         # SQLite 状态存储（活动去重 + 执行记录）
├── notifier.py      # 邮件通知
└── webui/           # FastAPI + 单页前端
```

## ⚠️ 已知限制

- 活动推图依赖 BAAS 社区维护的活动模块（`module/activities/`，每个活动一个插件）。GameKee 活动标题为中文，无法自动对应模块名，新活动需在配置中指定 `baas.current_activity`；未指定时记录活动并跳过推图
- 实际运行需 Windows + 模拟器 + 完整 BAAS 环境；调度器本身可在任意平台开发测试（不 import core 的部分）
- BAAS 上游 release 包与源码偶尔字段不同步（如 v1.4.3 的 `steam_app_process_name` vs 源码 `PC_app_process_name`），BAAS-Plus 首次调用时会自动对齐 `config/static.json` 与 `config/<server>/config.json` 字段（幂等，保留用户已有配置）

## 📄 许可证

[AGPL-3.0](LICENSE)

- BAAS 本体为 GPL-3.0，本项目仅作为外部依赖调用，未修改其源码（AGPL §13 允许与 GPL-3 作品结合）
- 活动数据源逻辑参考 [BlueArchive.ics](https://github.com/This-is-XiaoDeng/BlueArchive.ics)（同作者，AGPL-3.0）
