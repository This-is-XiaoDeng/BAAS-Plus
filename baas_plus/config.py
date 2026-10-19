"""配置模型：pydantic v2 定义 + JSON 加载/保存

配置文件默认位于 data/config.json（可通过环境变量 BAAS_PLUS_CONFIG 覆盖）。

多账号结构：AppConfig 持有账号列表（accounts），每个账号拥有独立的模拟器
实例 / BAAS 配置 / 任务 / 扫荡；活动设置（活动策略、当前活动模块、轮播图
区域、资源补全等，ActivityConfig）与 webui、notify（SMTP 发件）、data_dir
一样为**全局项**，所有账号共用。旧版单账号配置（顶层 simulator/baas/
activity/sweep）在加载时自动迁移到 accounts[0]，活动设置提升为全局
（见 _migrate_legacy_config）。

游戏内切换账号（AccountSwitchConfig，全局）：开启后多账号不再「一账号一个
模拟器多开实例」，而是共用同一个模拟器实例，在每个账号执行开始时通过游戏
登录界面（Yostar 账号选择框）切换游戏账号；每个账号需配置自身在 Yostar
下拉列表中的序号（AccountConfig.game_account_index，从 1 开始）。
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
    # 服务器：cn / in / jp（影响 BAAS 内部判断与活动数据源抓取）
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


class ActivityConfig(BaseModel):
    """活动数据源与推图策略（**全局设置**，所有账号共用，不按账号区分）

    旧版中 current_activity / banner_region 位于 BaasConfig、本模型按账号配置；
    现统一提升为全局（AppConfig.activity），旧配置加载时自动迁移。
    """

    data_source: Literal["gamekee"] = "gamekee"  # 数据源（内置 GameKee 抓取，参考 BlueArchive.ics）
    # 手动指定 BAAS 活动模块名（对应 module/activities/<name>.py）；留空则由引擎从活动数据自动推断
    current_activity: str = ""
    # 主页活动轮播图区域（1280x720 分辨率下的 [x1, y1, x2, y2]，用于 OCR 识别当前横幅）
    banner_region: list[int] = Field(default_factory=lambda: [1109, 133, 1280, 281])
    # 检测到未推送过的新活动时，是否自动执行活动推图
    push_story_on_new: bool = True
    push_mission_on_new: bool = False
    push_challenge_on_new: bool = False
    # 活动扫荡前是否先推图（explore_activity_mission 全推至 SSS）：
    # BAAS 定位任务依赖按钮模板匹配，未解锁（未推）任务的按钮样式不匹配，
    # 直接扫荡会定位失败；已全 SSS 时推图会快速跳过，开销很小
    push_before_sweep: bool = True
    # BAAS 尚未收录当前服活动截图资源时，用 BAAS-Plus 自己准备的模板在运行时
    # 注入 BAAS 的 image_dic / image_x_y_range（内存注入，不修改 BAAS 源码）。
    # 资源在 WebUI「活动 → 活动资源」里检查/补齐（上传现场截图裁剪，见
    # baas_plus/activity_assets.py）；默认关闭 = 缺资源时直接跳过活动推图/扫荡。
    inject_activity_resources: bool = False


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
    # 无活动进行中时，普通/困难图扫完后用剩余体力扫荡特别委托
    # （BAAS 任务 clear_special_task_power：据点防御 / 信用回收）
    special_task_when_no_activity: bool = False
    # 特别委托扫荡次数（BAAS 配置 special_task_times 原生格式："据点防御,信用回收"，
    # 每项为数字或 max；max 时 BAAS 按当前剩余体力点满扫荡，体力不足自动停止）
    special_task_times: str = Field(
        default="0,max", pattern=r"^\s*(?:max|\d+)\s*,\s*(?:max|\d+)\s*$"
    )


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
    # 执行完成后：回游戏主界面 → adb 截取模拟器画面 → 作为内联图片嵌入汇总邮件
    # （走 BAAS 桥接层，无需额外浏览器；截图失败仅注明，不阻断邮件发送）
    attach_game_screenshot: bool = False


class AccountSwitchConfig(BaseModel):
    """游戏内切换账号（全局设置，替代「一账号一个模拟器多开实例」）

    开启后多账号共用同一个模拟器实例：每个账号执行开始时先回到游戏登录界面，
    通过「切换账号 → 确定 → Yostar 账号选择框」点选自己的游戏账号再进入游戏。
    坐标均为 1280x720 基准（baas.click 内部按实际分辨率缩放），默认值取自
    实机截图标注（登录界面 / 确认弹窗 / Yostar 选择框）。
    """

    enabled: bool = False  # 开启后各账号 game_account_index 必须互不相同且 >= 1
    # 登录界面左下角「切换账号」按钮
    switch_button: tuple[int, int] = (60, 688)
    # 「您确定要切换账号吗?」确认弹窗的「确定」按钮
    confirm_button: tuple[int, int] = (736, 415)
    # 登出后的标题过渡页：点击空白处打开 Yostar 账号选择框
    blank_click: tuple[int, int] = (640, 360)
    # Yostar 账号选择框：账号条目右侧的下拉箭头（展开账号列表）
    dropdown_button: tuple[int, int] = (855, 315)
    # Yostar 账号选择框：「进入游戏」按钮
    enter_game_button: tuple[int, int] = (642, 430)
    # 展开后的账号列表中第一个账号条目（第 n 个账号为其 y + (n-1)×行高）
    first_account_pos: tuple[int, int] = (600, 420)
    # 账号条目的纵向间距（实测相邻两条目 y 相差 100px）
    account_row_height: int = 100
    # 重启 BA 后等待登录界面出现的最长时间（秒；模拟器性能差/首次冷启动时调大）
    app_start_timeout: float = 120.0
    # 登出后等待 Yostar 账号选择框出现的最长时间（秒）
    dialog_timeout: float = 60.0
    # 疑似「自动登录」的盲等窗口（秒）：重启游戏后持续这么久识别不到任何已知
    # 界面特征（登录界面/选择框/标题横幅），判定游戏带着当前账号自动登录直接
    # 进了主界面 → 再次杀进程重启复位（最多 2 次）。冷启动加载慢的机器调大，
    # 避免把还在加载的游戏误杀重启（重启反而更慢）
    blind_restart_after: float = 120.0


class WebUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18080


class AccountConfig(BaseModel):
    """单个账号的完整执行配置

    多账号时每个账号独立：模拟器多开实例（游戏内切换模式下改为共用同一实例）、
    Yostar 游戏账号序号、BAAS 配置目录、任务勾选与扫荡策略；活动设置为全局
    （AppConfig.activity），通知收件人可用 notify_to_addrs 覆盖全局（None = 用全局）。
    """

    # 稳定标识：改名不影响执行记录/活动状态关联（创建时自动生成，勿手改）
    id: str = Field(default_factory=lambda: f"acc_{uuid.uuid4().hex[:8]}")
    name: str = "默认账号"
    enabled: bool = True  # 是否参与批量执行（run 不带 --account 时）
    # 本账号在 Yostar 登录界面「账号下拉列表」中的序号（1 起，从上往下数）。
    # 仅游戏内切换账号模式（AppConfig.account_switch.enabled）下使用；0 = 未设置
    # （切换模式下校验会拒绝，避免所有账号默认落在第 1 个游戏账号把日志打错号）。
    game_account_index: int = Field(default=0, ge=0)
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    baas: BaasConfig = Field(default_factory=BaasConfig)
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
    """全局配置：账号列表 + 全局项（活动设置 / WebUI / 通知 SMTP / 数据目录）"""

    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    # 活动数据源与推图策略（全局设置，所有账号共用）
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
    # 游戏内切换账号（全局设置：开启后多账号共用一个模拟器实例，靠游戏内
    # 「切换账号」轮换游戏账号，不再为每个账号开一个模拟器）
    account_switch: AccountSwitchConfig = Field(default_factory=AccountSwitchConfig)
    # 数据目录（活动状态、执行记录 SQLite 与配置文件的父目录）
    data_dir: str = "data"
    # 多次执行：全部账号执行完一轮后立即开始下一轮（无间隔），循环 run_times 轮。
    # 默认 1 = 只执行一轮（与旧版行为一致）；CLI 可用 --times 覆盖，WebUI 在主操作条设置。
    run_times: int = Field(default=1, ge=1)
    accounts: list[AccountConfig] = Field(default_factory=lambda: [AccountConfig()])

    # ---- 兼容属性：代理到默认账号（accounts[0]）----
    # 旧代码/旧测试直接访问 config.simulator / config.baas / config.sweep 时仍可用
    # （多账号下应改用 accounts 列表）；activity 已是真实全局字段，无需代理。
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
        # 游戏内切换模式的合法性：同一模拟器实例 + 唯一且已设置的 Yostar 账号序号
        # （不满足时游戏内切换会点错账号 / 打到同一个号，宁可配置阶段就报错）
        if self.account_switch.enabled:
            indexes = [a.game_account_index for a in self.accounts]
            if any(i < 1 for i in indexes):
                raise ValueError(
                    "游戏内切换账号模式下，每个账号必须设置游戏账号序号"
                    "（game_account_index，Yostar 下拉列表中从上往下的序号，从 1 开始）"
                )
            if len(indexes) != len(set(indexes)):
                raise ValueError(f"游戏账号序号必须互不相同: {indexes}")
            if len(self.accounts) > 1:
                sim_keys = {(a.simulator.type, a.simulator.instance) for a in self.accounts}
                if len(sim_keys) > 1:
                    raise ValueError(
                        "游戏内切换账号模式下，所有账号必须共用同一个模拟器实例"
                        "（模拟器类型与多开编号完全一致），不能再多开"
                    )
        return self

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        return p


# 旧版单账号配置中属于账号的顶层字段
LEGACY_ACCOUNT_FIELDS = ("simulator", "baas", "activity", "sweep")

# 已全局化的活动字段（旧版位于账号 baas 子对象中，现归入全局 ActivityConfig）
_GLOBAL_ACTIVITY_BAAS_KEYS = ("current_activity", "banner_region")


def _promote_global_activity(data: dict) -> dict:
    """把账号里的活动设置提升为全局 activity（活动设置不再按账号配置）

    全局 activity 缺失时，取 accounts[0] 的活动配置（activity 对象 +
    baas.current_activity / baas.banner_region）作为全局值；多账号下以
    accounts[0] 为准（旧版 WebUI 保存时各账号的活动设置本就同步为相同值）。
    仅在确有内容可提升时返回新 dict，否则原样返回（不做破坏性改写）。
    """
    if "activity" in data:
        return data
    accounts = data.get("accounts") or []
    first = accounts[0] if accounts else None
    if not isinstance(first, dict):
        return data
    act = dict(first.get("activity") or {})
    baas = first.get("baas")
    if isinstance(baas, dict):
        for key in _GLOBAL_ACTIVITY_BAAS_KEYS:
            if baas.get(key) is not None and key not in act:
                act[key] = baas[key]
    act.pop("server", None)  # server 不再属于活动配置（跟随各账号 baas.server）
    if not act:
        return data
    migrated = dict(data)
    migrated["activity"] = act
    return migrated


def _migrate_legacy_config(data: dict) -> dict:
    """旧版单账号配置（顶层 simulator/baas/activity/sweep）→ 新结构 accounts[0]

    升级前 data/config.json 把账号配置放在顶层；升级后搬到 accounts[0]
    （id 固定 acc_default，name="默认账号"）。notify/webui/data_dir 保持全局。
    活动设置（activity 及 baas.current_activity / baas.banner_region）不再按
    账号配置，统一提升为全局 activity（见 _promote_global_activity）。
    已是最新结构（无账号级活动设置可提升）时原样返回，不做破坏性改写。
    """
    if "accounts" not in data:
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
        data = migrated
    return _promote_global_activity(data)


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
