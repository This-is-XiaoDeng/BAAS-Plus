"""活动数据源测试（mock httpx，不访问真实网络）"""
import time

import httpx
import pytest

from baas_plus.activity import ActivityFetcher, EventType, GameEvent

EVENT_ITEM = {
    "id": 1001,
    "title": "示例活动",
    "begin_at": int(time.time()) - 3600,
    "end_at": int(time.time()) + 86400,
    "picture": "http://x/1.png",
}
CARD_ITEM = {
    "id": 2001,
    "name": "示例卡池",
    "start_at": int(time.time()) - 3600,
    "end_at": int(time.time()) + 86400,
    "icon": "http://x/2.png",
}


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request):
        return await self._handler(request)


def make_client(handler):
    transport = FakeTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def test_fetch_activities():
    async def handler(request):
        assert request.url.path.endswith("/activity/page-list")
        return httpx.Response(200, json={"data": [EVENT_ITEM]})

    fetcher = ActivityFetcher("cn")
    fetcher._fetch_json = None  # type: ignore[assignment]
    # 直接注入 mock client
    async def mock_fetch(url, params):
        async with make_client(handler) as client:
            resp = await client.get(url, params=params)
            return resp.json()

    fetcher._fetch_json = mock_fetch  # type: ignore[assignment]
    events = await fetcher.fetch_activities()
    assert len(events) == 1
    assert events[0].event_type == EventType.EVENT
    assert events[0].title == "示例活动"
    assert events[0].is_active


async def test_fetch_all_dedupe():
    """总力战与活动同 id 时去重，总力战优先"""
    now = int(time.time())
    assault_item = {**EVENT_ITEM, "id": 3001}
    activity_item = {**EVENT_ITEM, "id": 3001}  # 相同 id
    calls = []

    async def handler(request):
        path = request.url.path
        calls.append(path)
        if "cardPool" in path:
            return httpx.Response(200, json={"data": [CARD_ITEM]})
        if "activity_kind_id" in request.url.params:
            return httpx.Response(200, json={"data": [assault_item]})
        return httpx.Response(200, json={"data": [activity_item]})

    fetcher = ActivityFetcher("cn")

    async def mock_fetch(url, params):
        async with make_client(handler) as client:
            resp = await client.get(url, params=params)
            return resp.json()

    fetcher._fetch_json = mock_fetch  # type: ignore[assignment]
    events = await fetcher.fetch_all()
    types = [e.event_type for e in events]
    assert types.count(EventType.ASSAULT) == 1
    assert types.count(EventType.EVENT) == 0  # 同 id 被总力战去重
    assert types.count(EventType.CARD) == 1


def test_game_event_key():
    e = GameEvent(id=1, title="t", start_at=0, end_at=0, event_type=EventType.EVENT)
    assert e.key == "event:1"


def test_sweepable_only_event():
    """仅常规活动需要扫荡；总力战/大决战/卡池不需要"""
    event = GameEvent(id=1, title="活动", start_at=0, end_at=9999999999, event_type=EventType.EVENT)
    assault = GameEvent(id=2, title="总力战", start_at=0, end_at=9999999999, event_type=EventType.ASSAULT)
    card = GameEvent(id=3, title="卡池", start_at=0, end_at=9999999999, event_type=EventType.CARD)
    assert event.is_sweepable
    assert not assault.is_sweepable
    assert not card.is_sweepable


def test_fetcher_invalid_server():
    with pytest.raises(ValueError):
        ActivityFetcher("xx")
