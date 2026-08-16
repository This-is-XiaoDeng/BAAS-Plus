"""配置模型：pydantic v2 定义 + JSON 加载/保存

配置文件默认位于 data/config.json（可通过环境变量 BAAS_PLUS_CONFIG 覆盖）。

多账号结构：AppConfig 持有账号列表（accounts），每个账号拥有独立的模拟器
实例 / BAAS 配置 / 任务 / 扫荡 / 活动策略；webui、notify（SMTP 发件）、
data_dir 为全局项。旧版单账号配置（顶层 simulator/baas/activity/sweep）在
加载时自动迁移到 accounts[0]（见 _migrate_legacy_config）。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

DEFAULT_CONFIG_PATH = os.environ.get(
    "BAAS_PLUS_CONFIG", str(Path(__file__).resolve().parent.parent / "data" / "config.json")
)

# BAAS 支持的全部可勾选任务（Baas_thread.funcs 的子集，按日常使用频率排序）
BAAS_TASKS: list[str] = [
    "cafe_reward",  # 咖啡厅
    "lesson",  # 课程表
    "collect_reward",  # 领取奖励
    "collect_daily_free_power",  # 每日免费体力
    "group",  # 社团
    "mail",  # 邮件
    "friend",  # 好友
    "main_story",  # 主线剧情
    "group_story",  # 社团剧情
    "mini_story",  # 小故事
    "scrimmage",  # 演习
    "arena",  # 竞技场
    "joint_firing_drill",  # 联合作战
    "rewarded_task",  # 悬赏通缉
    "clear_special_task_power",  # 特别委托
    "create",  # 制造
    "dailyGameActivity",  # 每日活动
    "explore_normal_task",  # 推图-普通
    "explore_hard_task",  # 推图-困难
    "normal_task",  # 扫荡-普通
    "hard_task",  # 扫荡-困难
    "activity_sweep",  # 扫荡-活动
    "de_clothes",  # 反和谐
    "total_assault",  # 总力战（BAAS master 已禁用）
]

# 任务中文名（WebUI 展示用）
TASK_LABELS: dict[str, str] = {
    "cafe_reward": "咖啡厅",
    "lesson": "课程表",
    "collect_reward": "领取奖励",
    "collect_daily_free_power": "每日免费体力",
    "group": "社团",
    "mail": "邮件",
    "friend": "好友",
    "main_story": "主线剧情",
    "group_story": "社团剧情",
    "mini_story": "小故事",
    "scrimmage": "演习",
    "arena": "竞技场（自动重复至票用完）",
    "joint_firing_drill": "联合作战",
    "rewarded_task": "悬赏通缉",
    "clear_special_task_power": "特别委托",
    "create": "制造",
    "dailyGameActivity": "每日活动",
    "explore_normal_task": "推图-普通",
    "explore_hard_task": "推图-困难",
    "normal_task": "扫荡-普通",
    "hard_task": "扫荡-困难",
    "activity_sweep": "扫荡-活动",
    "de_clothes": "反和谐",
    "total_assault": "总力战（已禁用）",
}

# 扫荡类任务：由引擎扫荡阶段统一调度（按体力算次数），勾选后不会在任务阶段重复执行
SWEEP_TASKS = {"normal_task", "hard_task", "activity_sweep"}


class SimulatorConfig(BaseModel):
    """模拟器配置（复用 BAAS 的 emulator_manager 支持类型）"""

    type: str = "mumu"  # mumu / mumu_global / leidian / bluestacks_nxt / ...
    instance: int = 0  # 多开编号，从 0 开始（多账号时每个账号一个实例）
    # 读取体力失败（截图帧为空，通常意味着模拟器/游戏失联）时，自动重启一次模拟器
    # 并重新启动游戏后重试；仍失败则跳过扫荡，不中断整个执行
    auto_restart_on_failure: bool = True


class BaasConfig(BaseModel):
    """BAAS 本体集成配置"""

    # BAAS 仓库本地路径；留空则尝试从已安装包导入（poetry extra: baas）
    repo_dir: str = ""
    # BAAS 的配置目录名（BAAS 根目录 config/ 下的子目录，release 包自带 cn/global/jp/steam）
    config_dir: str = "cn"
    # 服务器：cn / in / jp（影响活动数据与 BAAS 内部判断）
    server: Literal["cn", "in", "jp"] = "cn"
    # BA 游戏包名（覆盖 BAAS 内置服务器→包名映射；默认国服官服）
    game_package_name: str = "com.RoamingStar.BlueArchive"
    # 勾选要执行的任务（顺序执行）
    tasks: list[str] = Field(
        default_factory=lambda: [
            "cafe_reward",
            "lesson",
            "collect_reward",
            "collect_daily_free_power",
            "group",
            "mail",
            "friend",
        ]
    )
    # 手动指定 BAAS 活动模块名（对应 module/activities/<name>.py）；留空则由引擎从活动数据自动推断
    current_activity: str = ""
    # 主页活动轮播图区域（1280x720 分辨率下的 [x1, y1, x2, y2]，用于 OCR 识别当前横幅）
    banner_region: list[int] = Field(default_factory=lambda: [1109, 133, 1280, 281])


class ActivityConfig(BaseModel):
    """活动数据源与推图策略"""

    data_source: Literal["gamekee"] = "gamekee"  # 数据源（内置 GameKee 抓取，参考 BlueArchive.ics）
    server: Literal["cn", "in", "jp"] = "cn"
    # 检测到未推送过的新活动时，是否自动执行活动推图
    push_story_on_new: bool = True
    push_mission_on_new: bool = False
    push_challenge_on_new: bool = False
    # 活动扫荡前是否先推图（explore_activity_mission 全推至 SSS）：
    # BAAS 定位任务依赖按钮模板匹配，未解锁（未推）任务的按钮样式不匹配，
    # 直接扫荡会定位失败；已全 SSS 时推图会快速跳过，开销很小
    push_before_sweep: bool = True


class SweepConfig(BaseModel):
    """扫荡策略"""

    # auto：按剩余体力计算次数；fixed：使用固定次数
    strategy: Literal["auto", "fixed"] = "auto"
    fixed_times: int = 5  # strategy=fixed 时的扫荡次数
    max_times: int = 20  # auto 模式下单关最多扫荡次数上限
    # 有活动时优先扫荡活动关卡（调用 BAAS activity_sweep，-1 = 按 AP 自动全扫）
    activity_first: bool = True
    # 活动扫荡关卡号（BAAS 配置 activity_sweep_task_number，支持 "1,2,3"）
    activity_task_number: str = "1"
    # 无活动时扫荡的普通图/困难图列表，格式 region-mission-counts（counts 可为数字或 max），
    # 如 ["15-1-3", "16-3-5"]；strategy=auto 时 counts 会被引擎按剩余体力重算
    normal_tasks: list[str] = Field(default_factory=list)
    hard_tasks: list[str] = Field(default_factory=list)


class EmailConfig(BaseModel):
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""  # 发件邮箱
    password: str = ""  # SMTP 授权码（QQ 邮箱需在设置中生成）
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)


class NotifyConfig(BaseModel):
    """全局通知配置（SMTP 发件只有一个；收件人可按账号用 notify_to_addrs 覆盖）"""

    enabled: bool = False
    email: EmailConfig = Field(default_factory=EmailConfig)


class WebUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18080


class AccountConfig(BaseModel):
    """单个账号的完整执行配置（账号 = 一个模拟器实例 + 一套 BAAS 配置）

    多账号时每个账号独立：模拟器多开实例、BAAS 配置目录、任务勾选、扫荡与
    活动策略；通知收件人可用 notify_to_addrs 覆盖全局（None = 用全局）。
    """

    # 稳定标识：改名不影响执行记录/活动状态关联（创建时自动生成，勿手改）
    id: str = Field(default_factory=lambda: f"acc_{uuid.uuid4().hex[:8]}")
    name: str = "默认账号"
    enabled: bool = True  # 是否参与批量执行（run 不带 --account 时）
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    baas: BaasConfig = Field(default_factory=BaasConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
    sweep: SweepConfig = Field(default_factory=SweepConfig)
    # 通知收件人覆盖（None = 使用全局 notify.email.to_addrs）
    notify_to_addrs: Optional[list[str]] = None

    @model_validator(mode="after")
    def _check_tasks(self) -> "AccountConfig":
        unknown = [t for t in self.baas.tasks if t not in BAAS_TASKS]
        if unknown:
            raise ValueError(f"未知任务: {unknown}，可选: {BAAS_TASKS}")
        return self


class AppConfig(BaseModel):
    """全局配置：账号列表 + 全局项（WebUI / 通知 SMTP / 数据目录）"""

    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    # 数据目录（活动状态、执行记录 SQLite 与配置文件的父目录）
    data_dir: str = "data"
    accounts: list[AccountConfig] = Field(default_factory=lambda: [AccountConfig()])

    # ---- 兼容属性：代理到默认账号（accounts[0]）----
    # 旧代码/旧测试直接访问 config.simulator / config.baas / config.activity /
    # config.sweep 时仍可用（多账号下应改用 accounts 列表）。
    @property
    def simulator(self) -> SimulatorConfig:
        return self.accounts[0].simulator

    @simulator.setter
    def simulator(self, value: SimulatorConfig) -> None:
        self.accounts[0].simulator = value

    @property
    def baas(self) -> BaasConfig:
        return self.accounts[0].baas

    @baas.setter
    def baas(self, value: BaasConfig) -> None:
        self.accounts[0].baas = value

    @property
    def activity(self) -> ActivityConfig:
        return self.accounts[0].activity

    @activity.setter
    def activity(self, value: ActivityConfig) -> None:
        self.accounts[0].activity = value

    @property
    def sweep(self) -> SweepConfig:
        return self.accounts[0].sweep

    @sweep.setter
    def sweep(self, value: SweepConfig) -> None:
        self.accounts[0].sweep = value

    @model_validator(mode="after")
    def _check_accounts(self) -> "AppConfig":
        if not self.accounts:
            raise ValueError("至少需要一个账号")
        ids = [a.id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError(f"账号 id 重复: {ids}")
        return self

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        return p


# 旧版单账号配置中属于账号的顶层字段
LEGACY_ACCOUNT_FIELDS = ("simulator", "baas", "activity", "sweep")


def _migrate_legacy_config(data: dict) -> dict:
    """旧版单账号配置（顶层 simulator/baas/activity/sweep）→ 新结构 accounts[0]

    升级前 data/config.json 把账号配置放在顶层；升级后搬到 accounts[0]
    （id 固定 acc_default，name="默认账号"）。notify/webui/data_dir 保持全局。
    已是新结构（含 accounts）或非旧配置时原样返回，不做任何破坏性改写。
    """
    if "accounts" in data:
        return data
    if not any(k in data for k in LEGACY_ACCOUNT_FIELDS):
        return data
    migrated = dict(data)
    account: dict[str, object] = {
        "id": "acc_default",
        "name": "默认账号",
        "enabled": True,
    }
    for key in LEGACY_ACCOUNT_FIELDS:
        if key in migrated:
            account[key] = migrated.pop(key)
    if "notify_to_addrs" in migrated:  # 防御：旧配置不应出现，出现则归入账号
        account["notify_to_addrs"] = migrated.pop("notify_to_addrs")
    migrated["accounts"] = [account]
    return migrated


def load_config(path: str | None = None) -> AppConfig:
    """加载配置；文件不存在时返回默认配置（旧版单账号结构自动迁移）"""
    cfg_path = Path(path or DEFAULT_CONFIG_PATH)
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return AppConfig.model_validate(_migrate_legacy_config(raw))
    return AppConfig()


def save_config(config: AppConfig, path: str | None = None) -> Path:
    """保存配置到 JSON 文件（自动创建目录）"""
    cfg_path = Path(path or DEFAULT_CONFIG_PATH)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        config.model_dump_json(indent=2), encoding="utf-8"
    )
    return cfg_path
