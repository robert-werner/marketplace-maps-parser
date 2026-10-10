"""Browser API scope, pagination, failure and cleanup contracts."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from domain.entities import ProductRef
from infrastructure.marketplaces.avito import AvitoAdapter
from infrastructure.marketplaces.wildberries import WildberriesAdapter
from infrastructure.transports import avito_browser, two_gis_browser
from infrastructure.transports.browser_api import (
    BrowserApiError,
    ResponseCapture,
    fetch_json,
    same_endpoint_url,
)
from infrastructure.transports.wb_api import (
    feedback_cards,
    feedback_next_url,
    is_feedback_url,
)
from infrastructure.transports.yandex_browser import YandexBrowserTransport
from infrastructure.transports.yandex_maps_browser import (
    YandexMapsBrowserTransport,
)
from infrastructure.transports.yandex_market_fast import (
    iter_market_state,
    market_cards,
)
from marketplace_maps_parser.cli_args import parse_args
from shared.url_parsers import detect_marketplace, extract_avito_profile_id

AVITO = "https://www.avito.ru/brands/i219481394/all"
ENDPOINT = "https://www.avito.ru/web/7/user/test-user/ratings"


def test_avito_cli_detection_and_browser_only_transport():
    args = parse_args(["--url", AVITO, "--no-browser-api"])
    assert args.marketplace == "avito"
    assert args.no_browser_api
    assert args.transport == "playwright"
    assert extract_avito_profile_id(AVITO) == "i219481394"


@pytest.mark.parametrize("url", [
    "https://avito.ru.evil.test/brands/i123/all",
    "https://evil-avito.ru/brands/i123/all",
    "https://avito.ru/moskva/noutbuki",
    "https://www.avito.ru/brands/not-an-id/all",
    "https://www.avito.ru/brands/i123/all/other",
])
def test_avito_rejects_foreign_hosts_and_unsupported_paths(url):
    assert detect_marketplace(url) is None
    with pytest.raises(ValueError):
        extract_avito_profile_id(url)


@pytest.mark.parametrize("candidate", [
    "https://other.test/web/7/user/test-user/ratings?token=secret",
    "//other.test/web/7/user/test-user/ratings",
    "/web/7/user/different-user/ratings",
    "http://www.avito.ru/web/7/user/test-user/ratings",
])
def test_cursor_cannot_send_tokens_outside_original_endpoint(candidate):
    with pytest.raises(BrowserApiError):
        same_endpoint_url(ENDPOINT, candidate)


def test_relative_cursor_preserves_same_endpoint():
    assert same_endpoint_url(ENDPOINT, "?offset=25") == ENDPOINT + "?offset=25"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (403, {"entries": []}), (429, {}), (200, None), (200, []),
])
async def test_browser_fetch_rejects_block_and_non_object(status, payload):
    class Page:
        async def evaluate(self, script, args):
            assert "AbortController" in script
            assert "clearTimeout" in script
            assert args["timeoutMs"] == 100
            assert args["credentials"] == "include"
            return {"status": status, "payload": payload}

    with pytest.raises(BrowserApiError):
        await fetch_json(Page(), ENDPOINT, timeout_ms=100)


@pytest.mark.asyncio
async def test_cancellation_not_converted_into_api_fallback():
    class Page:
        async def evaluate(self, *args):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await fetch_json(Page(), ENDPOINT)


class CapturePage:
    def __init__(self):
        self.callback = None

    def on(self, event, callback):
        assert event == "response"
        self.callback = callback

    def remove_listener(self, event, callback):
        assert event == "response"
        assert callback == self.callback
        self.callback = None


def test_capture_is_bounded_filtered_and_unsubscribed():
    page = CapturePage()
    with ResponseCapture(page, lambda u: "reviews" in u, capacity=2) as cap:
        page.callback(SimpleNamespace(url="https://x.test/image"))
        for i in range(5):
            page.callback(SimpleNamespace(url=f"https://x.test/reviews/{i}"))
        assert [r.url for r in cap.responses] == [
            "https://x.test/reviews/3", "https://x.test/reviews/4",
        ]
    assert not cap.responses
    assert page.callback is None


def avito_entry(id_):
    return {
        "type": "rating",
        "value": {
            "id": id_, "score": 5, "title": "Buyer",
            "rated": "1 октября 2026",
            "textSections": [{"text": f"Review {id_}"}],
            "answer": {"textSections": [{"text": "Thanks"}]},
        },
    }


@pytest.mark.asyncio
async def test_avito_uses_discovered_api_and_closes_on_limit(monkeypatch):
    closed = False
    page = CapturePage()
    page.fetches = []

    class Response:
        url = ENDPOINT + "?limit=25&offset=25"
        status = 200

        async def json(self):
            return {
                "entries": [avito_entry(26)],
                "nextPage": None,
            }

    class Locator:
        first = None

        def __init__(self):
            self.first = self

        async def wait_for(self, **kwargs):
            pass

        async def count(self):
            return 1

        async def click(self, **kwargs):
            page.callback(Response())

    async def goto(*args, **kwargs):
        pass

    async def evaluate(script, args=None):
        if args is None:
            return {"total": 26, "average": 5, "title": "Shop", "cards": []}
        offset = parse_qs(urlsplit(args["url"]).query)["offset"][0]
        page.fetches.append(offset)
        assert offset == "0"  # Don't drop SSR's first 25 reviews.
        return {"status": 200, "payload": {
            "entries": [avito_entry(1), avito_entry(2)],
            "nextPage": "/web/7/user/test-user/ratings?limit=25&offset=25",
        }}

    page.goto, page.evaluate = goto, evaluate
    page.locator = lambda _: Locator()

    class Browser:
        async def new_page(self):
            return page

    class Session:
        async def __aenter__(self):
            return Browser()

        async def __aexit__(self, *args):
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        avito_browser, "import_invisible_playwright",
        lambda: lambda **kwargs: Session(),
    )
    adapter = AvitoAdapter(avito_browser.AvitoBrowserTransport(
        page_delay_seconds=0,
    ))
    iterator = adapter.iter_reviews(AVITO)
    review = await anext(iterator)
    assert review.review_id == "1"
    assert review.seller_answer == "Thanks"
    assert review.created_at.year == 2026
    await iterator.aclose()
    assert closed
    assert page.callback is None
    assert page.fetches == ["0"]


def test_wb_payload_maps_real_ids_fields_and_filters_other_variants():
    payload = {"feedbacks": [
        {"id": "r1", "nmId": 123, "productValuation": 4,
         "wbUserDetails": {"name": "Buyer"},
         "text": "Good", "pros": "Cheap", "cons": "Slow",
         "createdDate": "2026-01-02T03:04:05Z",
         "answer": {"text": "Thanks"},
         "photoLinks": [{"fullSize": "https://image.test/a.jpg"}]},
        {"id": "foreign", "nmId": 456, "productValuation": 5},
    ]}
    cards = feedback_cards(payload, 123)
    assert len(cards) == 1
    review = WildberriesAdapter._map_review(
        cards[0], ProductRef("wildberries", "https://x.test", "123"),
    )
    assert review.review_id == "r1"
    assert review.pros == "Cheap"
    assert review.cons == "Slow"
    assert review.seller_answer == "Thanks"
    assert review.created_at.isoformat() == "2026-01-02T03:04:05+00:00"
    assert not review.raw["dom"]


def test_wb_next_page_requires_server_cursor_and_same_endpoint():
    url = "https://feedbacks1.wb.ru/feedbacks/v2/123?cursor=one"
    assert is_feedback_url(url)
    assert not is_feedback_url("https://feedbacks1.wb.ru.evil.test/feedbacks")
    assert feedback_next_url(url, {"feedbacks": []}) is None
    assert feedback_next_url(url, {
        "feedbacks": [], "nextCursor": "two",
    }).endswith("cursor=two")
    with pytest.raises(BrowserApiError):
        feedback_next_url(url, {"nextPage": "https://evil.test/token"})


def market_data(ids, next_page="2"):
    return {
        "list": {
            "ugcItems": [{"reviewV2": {
                "id": id_, "author": {"nickname": "Buyer"}, "rating": 4,
                "comment": f"Review {id_}", "pro": "Pros", "contra": "Cons",
                "transition": {"params": {"oskuId": "123"}},
            }} for id_ in ids],
            "nextPageToken": next_page,
            "reviewStats": {"reviewsCount": 3},
        },
        "dates": [{"author": {"name": "Buyer"},
                   "datePublished": "2026-01-01"}],
        "title": "Product",
    }


def test_market_state_maps_ratings_and_filters_feed_items():
    data = market_data([1, 2])
    data["list"]["ugcItems"][1]["reviewV2"]["transition"]["params"] = {
        "oskuId": "456",
    }
    cards = market_cards(data, "123")
    assert len(cards) == 1
    assert cards[0]["rating"] == 4
    assert cards[0]["date"] == "2026-01-01"
    assert cards[0]["pros"] == "Pros"


def test_market_does_not_borrow_wrong_date_from_shifted_json_ld():
    data = market_data([1])
    data["list"]["ugcItems"][0]["reviewV2"]["descriptor"] = [
        {"type": "text", "content": "26 сентября"},
    ]
    # Same author, wrong day/month on a different review.
    assert market_cards(data, "123")[0]["date"] == "26 сентября"
    data["dates"][0]["datePublished"] = "2025-09-26"
    assert market_cards(data, "123")[0]["date"] == "2025-09-26"


def test_wb_unknown_product_reviews_do_not_leak_into_result():
    cards = feedback_cards({"feedbacks": [
        {"id": "other-product", "productValuation": 5},
    ]}, 123)
    assert cards == []


@pytest.mark.asyncio
async def test_market_warm_once_then_fetch_next_token_in_same_document():
    transport = YandexBrowserTransport(page_delay_seconds=0)
    state = {"seen": set(), "page_no": 1}

    class Handle:
        async def dispose(self):
            pass

    class Page:
        url = "https://market.yandex.ru/card/product/123/reviews"

        def __init__(self):
            self.navigations = 0
            self.fetches = []

        async def goto(self, *args, **kwargs):
            self.navigations += 1

        async def wait_for_function(self, *args, **kwargs):
            return Handle()

        async def evaluate(self, script, args=None):
            if args is None:
                return market_data([1, 2])
            assert "DOMParser" in script
            self.fetches.append(args["url"])
            return {"status": 200, "data": market_data([3], None)}

    page = Page()
    batches = [b async for b in iter_market_state(
        transport, page, "/card/product/123", state,
    )]
    assert [len(b) for b in batches] == [2, 1]
    assert page.navigations == 1
    assert page.fetches == [page.url + "?page=2"]
    assert transport.last_total_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("total,api_cards", [
    (1, None), (2, []), (2, [{"id": "r2", "text": "second", "rating": 5}]),
])
async def test_2gis_prefers_ssr_and_never_replaces_it_with_empty_api(
    monkeypatch, tmp_path, total, api_cards,
):
    page = CapturePage()
    page.gotos = []
    page.fetches = 0
    state = {"queries": [{
        "queryKey": ["fetchEntityReviews"],
        "state": {"data": {"pages": [{
            "items": [{"id": "r1", "rating": 5, "text": "first"}],
            "total": total, "hasMore": total > 1,
        }]}},
    }]}

    async def goto(url, **kwargs):
        page.gotos.append(url)
        page.callback(SimpleNamespace(
            url="https://public-api.reviews.2gis.com"
                "/3.0/branches/70000001063167147/reviews?limit=50",
        ))

    async def evaluate(script, args=None):
        if args is None:
            return json.dumps(state)
        page.fetches += 1
        return {"status": 200, "payload": {"reviews": api_cards}}

    async def no_wait(*args, **kwargs):
        pass

    page.goto, page.evaluate = goto, evaluate
    page.wait_for_timeout = no_wait

    class Browser:
        async def new_page(self):
            return page

    class Session:
        async def __aenter__(self):
            return Browser()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(
        two_gis_browser, "import_invisible_playwright",
        lambda: lambda **kwargs: Session(),
    )
    transport = two_gis_browser.TwoGisBrowserTransport(
        debug_dir=tmp_path, settle_ms=0, page_delay_seconds=0,
    )
    batches = [b async for b in transport.iter_review_batches(
        "https://2gis.ru/moscow/firm/70000001063167147",
    )]
    assert batches[0][0]["id"] == "r1"
    assert len(page.gotos) == 1
    assert page.fetches == (0 if total == 1 else 1)
    assert bool(transport.incomplete_reason) == (api_cards == [])
    assert page.callback is None


@pytest.mark.asyncio
async def test_maps_template_response_is_not_fetched_a_second_time():
    transport = YandexMapsBrowserTransport()
    transport.last_total_count = 2

    class Response:
        url = "https://yandex.ru/maps/api/business/fetchReviews?page=2"

        async def json(self):
            return {"data": {"reviews": [{"reviewId": "r2", "rating": 4}]}}

    class Page:
        async def evaluate(self, *args):
            pytest.fail("Captured response already completes the known total")

    seen = {"r1"}
    batches = [batch async for batch in transport._iter_direct_api(
        Page(), [Response()], seen, {},
    )]
    assert batches == [[{"reviewId": "r2", "rating": 4}]]
    assert seen == {"r1", "r2"}


@pytest.mark.asyncio
async def test_maps_empty_page_stops_without_walking_20_pages():
    transport = YandexMapsBrowserTransport(
        walk_extra_streams=False, api_concurrency=1, api_pacing_seconds=0,
    )
    calls = []

    class Response:
        url = (
            "https://yandex.ru/maps/api/business/fetchReviews"
            "?page=1&pageSize=50&csrfToken=public-page-token"
        )

        async def json(self):
            return {"data": {"reviews": []}}

    class Page:
        async def evaluate(self, script, url):
            assert "AbortSignal.timeout" in script
            calls.append(parse_qs(urlsplit(url).query))
            return {"status": 200, "payload": {
                "data": {"reviews": [], "params": {"limit": 100}},
            }}

    batches = [batch async for batch in transport._iter_direct_api(
        Page(), [Response()], set(), {},
    )]
    assert batches == []
    assert len(calls) <= 5  # probe + one empty page per ranking, not 20.


@pytest.mark.asyncio
async def test_parallel_market_limit_reaps_all_nested_producers():
    from marketplace_maps_parser.concurrency import _ParallelYandexSessions

    class Adapter:
        last_total_count = 1_000
        last_average_rating = 5
        last_product_title = "Test"

        def __init__(self):
            self.active = False

        async def iter_reviews(self, url):
            self.active = True
            try:
                for i in range(1_000):
                    yield i
            finally:
                await asyncio.sleep(0)
                self.active = False

    adapters = [Adapter(), Adapter()]
    stream = _ParallelYandexSessions(adapters).iter_reviews("unused")
    await anext(stream)
    await asyncio.wait_for(stream.aclose(), 1)
    assert not any(adapter.active for adapter in adapters)
