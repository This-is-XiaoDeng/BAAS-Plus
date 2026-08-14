"""活动数据源：内置 GameKee 抓取

数据来源为 GameKee 蔚蓝档案专区（https://www.gamekee.com/v1，game-alias: ba），
逻辑参考 BlueArchive.ics 项目（https://github.com/This-is-XiaoDeng/BlueArchive.ics）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

GAMEKEE_API_BASE = "https://www.gamekee.com/v1"
GAMEKEE_HEADERS = {"game-alias": "ba"}

SERVER_ID_MAP: dict[str, int] = {"cn": 16, "in": 17, "jp": 15}


# 活动标题关键词 → BAAS 活动模块名（人工映射，兜底英文关键词启发式匹配）
# 纯中文标题的活动靠这里命中；新活动确认后在此加一行即可
ACTIVITY_MODULE_ALIASES: dict[str, str] = {
    "笑笑闹闹": "livelyAndJoyfulWalkingTour",
    "走走绕绕": "livelyAndJoyfulWalkingTour",
    "来自歌剧的爱情": "FromOpera0068WithLove",
    "百芳丛中独一枝": "AHundredYearsofOneFlowerLetsGetRealwithaWaterBattle",
    "水上争锋": "AHundredYearsofOneFlowerLetsGetRealwithaWaterBattle",
}


class EventType(str, Enum):
    CARD = "card"  # 卡池
    EVENT = "event"  # 常规活动（GameKee activity_kind_id=14，BAAS 可推图/扫荡）
    ASSAULT = "assault"  # 总力战/大决战（kind=15）
    OTHER = "other"  # 掉落加成/总决算/无限制决战/综合战术测试/主线故事等（无需扫荡）


# GameKee activity_kind_id → 事件类型（仅 kind=14 常规活动可扫荡）
KIND_EVENT = 14
KIND_ASSAULT = 15
SWEEPABLE_KINDS = frozenset({KIND_EVENT})
# kind=14 中无关卡可扫的标题关键词（常驻化更新等纯公告）
NON_SWEEPABLE_TITLE_KEYWORDS = ("常驻化", "指引任务")


@dataclass(frozen=True)
class GameEvent:
    """游戏事件（活动）数据"""

    id: int
    title: str
    start_at: int  # Unix 时间戳
    end_at: int
    event_type: EventType
    picture: str = ""

    @property
    def is_active(self) -> bool:
        return self.end_at >= time.time()

    @property
    def is_sweepable(self) -> bool:
        """是否需要扫荡活动关卡：仅常规活动（总力战/大决战/卡池不需要）"""
        return self.event_type == EventType.EVENT

    @property
    def key(self) -> str:
        """本地去重键：同类型+同 id"""
        return f"{self.event_type.value}:{self.id}"


class ActivityFetcher:
    """GameKee 活动抓取器（异步）"""

    def __init__(self, server: str = "cn", timeout: float = 30.0) -> None:
        if server not in SERVER_ID_MAP:
            raise ValueError(f"不支持的服务器: {server}，可选值: cn, in, jp")
        self.server = server
        self.server_id = SERVER_ID_MAP[server]
        self.timeout = timeout

    async def _fetch_json(self, url: str, params: dict[str, Any]) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=GAMEKEE_HEADERS)
            resp.raise_for_status()
            return resp.json()

    async def fetch_card_pools(self) -> list[GameEvent]:
        """卡池"""
        data = await self._fetch_json(
            f"{GAMEKEE_API_BASE}/cardPool/query-list",
            {
                "order_by": "-1",
                "card_tag_id": "",
                "keyword": "",
                "kind_id": "6",
                "status": "0",
                "serverId": str(self.server_id),
            },
        )
        events = []
        for item in data.get("data", []):
            if item.get("end_at", 0) < time.time():
                continue
            events.append(
                GameEvent(
                    id=item.get("id", 0),
                    title=item.get("name", "未知卡池"),
                    start_at=item.get("start_at", 0),
                    end_at=item.get("end_at", 0),
                    event_type=EventType.CARD,
                    picture=item.get("icon", ""),
                )
            )
        return events

    async def fetch_activities(self) -> list[GameEvent]:
        """常规活动（按 activity_kind_id 精确分类：14=可扫荡活动，15=总力战/大决战，其余=无需扫荡）"""
        data = await self._fetch_json(
            f"{GAMEKEE_API_BASE}/activity/page-list",
            {
                "importance": "0",
                "sort": "-1",
                "keyword": "",
                "limit": "999",
                "page_no": "1",
                "serverId": str(self.server_id),
                "status": "0",
            },
        )
        events = []
        for item in data.get("data", []):
            if item.get("end_at", 0) < time.time():
                continue
            kind = item.get("activity_kind_id") or 0
            title = item.get("title", "未知活动")
            if kind in SWEEPABLE_KINDS and not any(kw in title for kw in NON_SWEEPABLE_TITLE_KEYWORDS):
                event_type = EventType.EVENT
            elif kind == KIND_ASSAULT:
                event_type = EventType.ASSAULT
            else:
                event_type = EventType.OTHER
            events.append(
                GameEvent(
                    id=item.get("id", 0),
                    title=title,
                    start_at=item.get("begin_at", 0),
                    end_at=item.get("end_at", 0),
                    event_type=event_type,
                    picture=item.get("picture", ""),
                )
            )
        return events

    async def fetch_total_assaults(self) -> list[GameEvent]:
        """总力战/大决战"""
        data = await self._fetch_json(
            f"{GAMEKEE_API_BASE}/activity/page-list",
            {
                "importance": "0",
                "sort": "-1",
                "keyword": "",
                "limit": "999",
                "page_no": "1",
                "serverId": str(self.server_id),
                "status": "0",
                "activity_kind_id": "15",
            },
        )
        events = []
        for item in data.get("data", []):
            if item.get("end_at", 0) < time.time():
                continue
            events.append(
                GameEvent(
                    id=item.get("id", 0),
                    title=item.get("title", "总力战"),
                    start_at=item.get("begin_at", 0),
                    end_at=item.get("end_at", 0),
                    event_type=EventType.ASSAULT,
                    picture=item.get("picture", ""),
                )
            )
        return events

    async def fetch_all(self) -> list[GameEvent]:
        """拉取全部事件（按 id 去重，总力战优先于活动——GameKee 中同一活动会同时出现在两个接口）"""
        import asyncio

        cards, activities, assaults = await asyncio.gather(
            self.fetch_card_pools(),
            self.fetch_activities(),
            self.fetch_total_assaults(),
        )
        seen: set[int] = set()
        unique: list[GameEvent] = []
        for event in assaults + activities + cards:
            if event.id not in seen:
                seen.add(event.id)
                unique.append(event)
        return unique
