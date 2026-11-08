"""WB public review API: card.json -> imt -> /feedbacks/v2 (no browser)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from infrastructure.transports import wb_public_api as api

FIX = Path(__file__).parent / "fixtures" / "wb"
CARD = json.loads((FIX / "card_831948063.json").read_text(encoding="utf-8"))
FEEDBACKS = json.loads(
    (FIX / "feedbacks_v2_917398157.json").read_text(encoding="utf-8"),
)
NM = 831948063
BASKET = "basket-38.wbbasket.ru"


@pytest.fixture(autouse=True)
def _real_collect(monkeypatch):
    """Undo the conftest guard: these tests drive collect() offline."""
    monkeypatch.setattr(api, "collect", _ORIGINAL_COLLECT)
    api._basket_cache.clear()
    monkeypatch.setattr(api.asyncio, "sleep", _no_sleep)


async def _no_sleep(_s):
    return None


_ORIGINAL_COLLECT = api.collect


def _router(feedback_status=200, feedback_payload=FEEDBACKS, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        host = request.url.host
        if host.endswith("wbbasket.ru"):
            if host == BASKET and request.url.path == api.card_path(NM):
                return httpx.Response(200, json=CARD)
            return httpx.Response(404)
        if host == "feedbacks1.wb.ru":
            return httpx.Response(feedback_status, json=feedback_payload)
        if host == "feedbacks2.wb.ru":
            return httpx.Response(200, json=feedback_payload)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_collect_resolves_imt_and_filters_by_nm():
    result = await api.collect(NM, transport=_router())
    assert result.imt_id == 917398157
    assert result.title == CARD["imt_name"]
    assert result.total_count == 41 and result.text_count == 27
    assert result.average_rating == pytest.approx(4.9)  # "4.9" string
    assert result.rating_only_count == 14
    assert result.returned == 4
    assert [c["id"] for c in result.cards] == [
        "iLU8cwMBZrqoqg3mbDZk", "B6tOiaWMghQYsFw6En89", "svtqThsFrmkNiNL0PJEx",
    ]  # the other-variant review (nmId 111) is dropped
    assert result.window_limited is False
    assert api._basket_cache[NM // 100_000] == f"https://{BASKET}"


async def test_collect_falls_back_to_mirror_on_5xx():
    calls: list[str] = []
    result = await api.collect(
        NM, transport=_router(feedback_status=503, calls=calls),
    )
    assert len(result.cards) == 3
    assert sum("feedbacks1.wb.ru" in c for c in calls) == 3  # retried
    assert any("feedbacks2.wb.ru" in c for c in calls)


async def test_null_feedbacks_means_no_reviews():
    empty = {"feedbackCount": 0, "feedbackCountWithText": 0,
             "valuation": "", "feedbacks": None}
    result = await api.collect(NM, transport=_router(feedback_payload=empty))
    assert result.cards == [] and result.average_rating is None


async def test_window_limited_flag():
    many = dict(FEEDBACKS)
    item = FEEDBACKS["feedbacks"][0]
    many["feedbacks"] = [
        {**item, "id": f"r{i}"} for i in range(api.FEEDBACK_WINDOW)
    ]
    many["feedbackCountWithText"] = 5000
    result = await api.collect(NM, transport=_router(feedback_payload=many))
    assert result.window_limited is True


async def test_card_not_found_raises():
    def handler(request):
        return httpx.Response(404)
    with pytest.raises(api.WbPublicApiError):
        await api.collect(NM, transport=httpx.MockTransport(handler))


def test_proxy_url_keeps_credentials():
    assert api.proxy_url(None) is None
    assert api.proxy_url({"server": "socks5://h:1"}) == "socks5://h:1"
    assert api.proxy_url({
        "server": "socks5://h:1", "username": "u@x", "password": "p:w",
    }) == "socks5://u%40x:p%3Aw@h:1"
    assert api.proxy_url({"server": "h:2", "username": "u"}) == "http://u@h:2"


async def test_warm_cache_guesses_neighbour_basket():
    calls: list[str] = []
    api._basket_cache[NM // 100_000 - 3] = f"https://{BASKET}"
    await api.collect(NM, transport=_router(calls=calls))
    basket_calls = [c for c in calls if "wbbasket.ru" in c]
    assert basket_calls[0].startswith(f"https://{BASKET}/")
    assert len(basket_calls) <= 4  # guess wave only, no 64-host fan-out
