"""配置模型：pydantic v2 定义 + JSON 加载/保存

配置文件默认位于 data/config.json（可通过环境变量 BAAS_PLUS_CONFIG 覆盖）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

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
    "clear_special_task_power",  # 特别依赖
    "create",  # 制造
    "dailyGameActivity",  # 每日活动
    "explore_normal_task",  # 推图-普通
    "explore_hard_task",  # 推图-困难
    "normal_task",  # 扫荡-普通
    "hard_task",  # 扫荡-困难
    "activity_sweep",  # 扫荡-活动
    "explore_activity_story",  # 活动推图-剧情
    "explore_activity_challenge",  # 活动推图-挑战
    "explore_activity_mission",  # 活动推图-任务
    "de_clothes",  # 脱衣服
    "total_assault",  # 总力战（BAAS master 已禁用）
]


class SimulatorConfig(BaseModel):
    """模拟器配置（复用 BAAS 的 emulator_manager 支持类型）"""

    type: str = "mumu"  # mumu / mumu_global / leidian / bluestacks_nxt / ...
    instance: int = 0  # 多开编号，从 0 开始


class BaasConfig(BaseModel):
    """BAAS 本体集成配置"""

    # BAAS 仓库本地路径；留空则尝试从已安装包导入（poetry extra: baas）
    repo_dir: str = ""
    # BAAS 的配置目录名（BAAS 根目录 config/ 下的子目录，release 包自带 cn/global/jp/steam）
    config_dir: str = "cn"
    # 服务器：cn / in / jp（影响活动数据与 BAAS 内部判断）
    server: Literal["cn", "in", "jp"] = "cn"
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


class ActivityConfig(BaseModel):
    """活动数据源与推图策略"""

    data_source: Literal["gamekee"] = "gamekee"  # 数据源（内置 GameKee 抓取，参考 BlueArchive.ics）
    server: Literal["cn", "in", "jp"] = "cn"
    # 检测到未推送过的新活动时，是否自动执行活动推图
    push_story_on_new: bool = True
    push_mission_on_new: bool = False
    push_challenge_on_new: bool = False


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
    enabled: bool = False
    email: EmailConfig = Field(default_factory=EmailConfig)


class WebUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18080


class AppConfig(BaseModel):
    """全局配置"""

    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    baas: BaasConfig = Field(default_factory=BaasConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
    sweep: SweepConfig = Field(default_factory=SweepConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    # 数据目录（活动状态、执行记录 SQLite 与配置文件的父目录）
    data_dir: str = "data"

    @model_validator(mode="after")
    def _check_tasks(self) -> "AppConfig":
        unknown = [t for t in self.baas.tasks if t not in BAAS_TASKS]
        if unknown:
            raise ValueError(f"未知任务: {unknown}，可选: {BAAS_TASKS}")
        return self

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        return p


def load_config(path: str | None = None) -> AppConfig:
    """加载配置；文件不存在时返回默认配置"""
    cfg_path = Path(path or DEFAULT_CONFIG_PATH)
    if cfg_path.exists():
        return AppConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    return AppConfig()


def save_config(config: AppConfig, path: str | None = None) -> Path:
    """保存配置到 JSON 文件（自动创建目录）"""
    cfg_path = Path(path or DEFAULT_CONFIG_PATH)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        config.model_dump_json(indent=2), encoding="utf-8"
    )
    return cfg_path
