"""活动数据源测试（mock httpx，不访问真实网络）"""
import time

import httpx
import pytest

from baas_plus import activity as activity_module
from baas_plus.activity import ActivityFetcher, EventType, GameEvent

EVENT_ITEM = {
    "id": 1001,
    "title": "示例活动",
    "begin_at": int(time.time()) - 3600,
    "end_at": int(time.time()) + 86400,
    "picture": "http://x/1.png",
    "activity_kind_id": 14,
}
CARD_ITEM = {
    "id": 2001,
    "name": "示例卡池",
    "start_at": int(time.time()) - 3600,
    "end_at": int(time.time()) + 86400,
    "icon": "http://x/2.png",
}
BONUS_ITEM = {  # 掉落加成公告（kind=16），不需要扫荡
    "id": 3001,
    "title": "【任务（普通难度）】道具掉落量3倍",
    "begin_at": int(time.time()) - 3600,
    "end_at": int(time.time()) + 86400,
    "activity_kind_id": 16,
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


async def test_fetch_activities_classifies_by_kind():
    """按 activity_kind_id 分类：14=可扫荡活动，16=掉落加成(其他)，15=总力战"""
    items = [
        {**EVENT_ITEM},
        {**BONUS_ITEM},
        {**EVENT_ITEM, "id": 4001, "title": "总力战：XXX", "activity_kind_id": 15},
    ]

    async def handler(request):
        return httpx.Response(200, json={"data": items})

    fetcher = ActivityFetcher("cn")

    async def mock_fetch(url, params):
        async with make_client(handler) as client:
            resp = await client.get(url, params=params)
            return resp.json()

    fetcher._fetch_json = mock_fetch  # type: ignore[assignment]
    events = await fetcher.fetch_activities()
    by_type = {e.id: e.event_type for e in events}
    assert by_type[1001] == EventType.EVENT
    assert by_type[3001] == EventType.OTHER  # 掉落加成不算可扫荡活动
    assert by_type[4001] == EventType.ASSAULT


async def test_kind14_title_blacklist():
    """kind=14 但标题含「常驻化」等关键词的纯公告不算可扫荡活动"""
    items = [
        {**EVENT_ITEM, "id": 5001, "title": "【龙武同舟】常驻化更新", "activity_kind_id": 14},
        {**EVENT_ITEM, "id": 5002, "title": "指引任务【XX】", "activity_kind_id": 14},
    ]

    async def handler(request):
        return httpx.Response(200, json={"data": items})

    fetcher = ActivityFetcher("cn")

    async def mock_fetch(url, params):
        async with make_client(handler) as client:
            resp = await client.get(url, params=params)
            return resp.json()

    fetcher._fetch_json = mock_fetch  # type: ignore[assignment]
    events = await fetcher.fetch_activities()
    assert all(e.event_type == EventType.OTHER for e in events)


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


def _patch_async_client(monkeypatch, handler):
    """把 ActivityFetcher._fetch_json 内部新建的 AsyncClient 换成假 transport"""
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real_client(transport=FakeTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)


async def test_fetch_json_empty_body_retries_then_reports_source(monkeypatch):
    """GameKee 返回 200 + 空 body（原始报错 Expecting value: line 1 column 1 (char 0)）：
    重试一次仍为空则抛指向数据源的 RuntimeError，报错里带接口地址"""
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="")

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(activity_module, "FETCH_RETRY_DELAY", 0)
    fetcher = ActivityFetcher("cn", timeout=1.0)
    with pytest.raises(RuntimeError, match="活动数据源（GameKee）返回异常"):
        await fetcher._fetch_json(f"{activity_module.GAMEKEE_API_BASE}/activity/page-list", {})
    assert calls["n"] == 2  # 空响应重试一次


async def test_fetch_json_retries_transient_empty_body(monkeypatch):
    """偶发空 body 后重试成功：不把数据源抖动抛给上层"""
    responses = [httpx.Response(200, text=""), httpx.Response(200, json={"data": [EVENT_ITEM]})]

    async def handler(request):
        return responses.pop(0)

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(activity_module, "FETCH_RETRY_DELAY", 0)
    fetcher = ActivityFetcher("cn", timeout=1.0)
    data = await fetcher._fetch_json(f"{activity_module.GAMEKEE_API_BASE}/activity/page-list", {})
    assert data == {"data": [EVENT_ITEM]}


async def test_fetch_json_non_json_body_reports_snippet(monkeypatch):
    """返回错误页 HTML 时：报错指出「不是 JSON」并附响应摘要（不抛裸 JSONDecodeError）"""

    async def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(activity_module, "FETCH_RETRY_DELAY", 0)
    fetcher = ActivityFetcher("cn", timeout=1.0)
    with pytest.raises(RuntimeError, match="响应不是 JSON"):
        await fetcher._fetch_json(f"{activity_module.GAMEKEE_API_BASE}/activity/page-list", {})


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
