# 🐱 BAAS-Plus

> 蔚蓝档案自动化调度器：自动开模拟器 → 检测新活动推图（仅已开始活动）→ 执行日常任务 → 按剩余体力扫荡 → 邮件通知 → 自动关模拟器

基于 [BAAS](https://github.com/pur1fying/blue_archive_auto_script) 构建，**不修改其源码**，仅通过官方 API 驱动。

## ✨ 特性

| | |
|---|---|
| 🤖 自动启动模拟器 | MuMu / 雷电等，自动获取 ADB 连接 |
| 🎉 新活动自动推图 | GameKee 数据源检测新活动，剧情/任务/挑战可分别开关 |
| 🎯 活动扫荡先进对活动 | 主页轮播图多活动混合轮播时，模板匹配确认当前页=目标活动再进入，避免进错活动；**扫荡前自动推图**（`push_before_sweep`，全推至 SSS）解锁任务后再扫荡 |
| ⚡ 按体力扫荡 | 任务完成后读取实时 AP 计算次数；活动扫荡自动选择**进行中**的活动（标题英文关键词 ↔ BAAS 活动模块），不会扫已结束/仅兑换可用的旧活动 |
| 🛡 实机检测敌人属性 | 推图进关前自动点击「敌人/克制」OCR 第一个敌人防御类型，按游戏内克制表修正 BAAS 关卡属性数据（重→贯穿、特殊→神秘等），避免官方 JSON 数据错误选错队 |
| 📧 邮件通知 | 执行结果推送到邮箱（SMTP，支持 QQ/163/网易企业邮授权码） |
| 📅 领取日程最后 | `collect_reward` 在所有任务（含竞技场）完成后执行，奖励档位全部解锁 |
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
> 示例中的 `D:\BAAS` 等路径请替换为你的实际目录。

### 定时执行（Windows 计划任务）

创建 `run.bat`：

```bat
@echo off
cd /d D:\BAAS
D:\BAAS\.venv\Scripts\python.exe -m baas_plus.cli run
```

> ⚠️ `cd /d` 到 BAAS 根目录是必须的（BAAS 用相对路径读 `config/`）；`.bat` 与任务计划程序都建议用 Python 完整路径。

注册每天 05:00 自动执行：

```
schtasks /create /tn "BAAS-Plus-Daily" /tr "D:\BAAS\run.bat" /sc daily /st 05:00
```

**或用图形界面（任务计划程序向导）**：

1. `Win+R` → `taskschd.msc` → 右侧「创建基本任务…」
2. 触发器：每天 → 时间（建议体力重置后，如 04:05）
3. 操作：启动程序 →
   - 程序/脚本：`D:\BAAS\.venv\Scripts\python.exe`
   - 添加参数：`-m baas_plus.cli run`
   - 起始于：`D:\BAAS`
4. 完成后双击任务 → 「条件」取消勾选「只有在计算机使用交流电源时才启动任务」（笔记本必改）；「设置」可设「运行超过 3 小时停止任务」防卡死

## 🖥 WebUI 配置

配置保存在 `data/config.json`，WebUI 中可视化编辑：

- **模拟器**：类型（mumu/雷电/蓝叠等）与多开编号
- **任务**：勾选要执行的日常任务（咖啡厅、课程表、邮件、竞技场等）
- **扫荡**：扫荡列表留空时自动从 BAAS 配置（mainlinePriority/hardPriority）读取；保存「模拟器&BAAS」设置后自动同步并显示
- **活动**：新活动自动推图开关，活动数据源选择；`push_before_sweep`（默认开）在活动扫荡前先推图解锁任务（已 SSS 的关卡快速跳过）
- **扫荡**：策略（`auto` 按体力算次数 / `fixed` 固定次数）、扫荡列表、活动关卡号
- **通知**：SMTP 服务器、邮箱账号、授权码、收件人
  - 常用服务器：QQ `smtp.qq.com:465`、163 `smtp.163.com:465`、**网易免费企业邮 `smtp.qiye.163.com:465`**
  - 密码填**客户端授权码**（非登录密码）；网易连续认证失败会临时锁定，勿频繁测试

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
- BAAS 的活动关卡属性数据（`src/explore_task_data/activities/*.json`）为社区手动录入，可能与实际不符（如笑笑闹闹 12 关全录成 shock）。BAAS-Plus 推图时实机检测敌人防御类型自动修正；**复合装甲**（对应新属性「分解」）BAAS 预设体系暂无对应，无法自动选队
- 实际运行需 Windows + 模拟器 + 完整 BAAS 环境；调度器本身可在任意平台开发测试（不 import core 的部分）
- BAAS 上游 release 包与源码偶尔字段不同步（如 v1.4.3 的 `steam_app_process_name` vs 源码 `PC_app_process_name`），BAAS-Plus 首次调用时会自动对齐 `config/static.json` 与 `config/<server>/config.json` 字段（幂等，保留用户已有配置）

## ⚠️ 免责声明

- 本项目为个人学习/自动化辅助工具，**非官方**，与 NEXON / Yostar 等无关；使用自动化脚本可能违反游戏用户协议，账号风险自负
- 实机自动化依赖模拟器分辨率与 BAAS 社区维护的截图模板，活动 UI 变化可能导致任务定位失败，请留意执行日志
- 邮件授权码、SMTP 配置等敏感信息保存在本地 `data/config.json`，请勿将 `data/` 目录提交或分享

## 📄 许可证

[AGPL-3.0](LICENSE)

- BAAS 本体为 GPL-3.0，本项目仅作为外部依赖调用，未修改其源码（AGPL §13 允许与 GPL-3 作品结合）
- 活动数据源逻辑参考 [BlueArchive.ics](https://github.com/This-is-XiaoDeng/BlueArchive.ics)（同作者，AGPL-3.0）
