"""Fast browser-only API path, bounded waits and deterministic cleanup."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.marketplaces.ozon_payload import (
    extract_review_id,
    iter_ozon_review_nodes,
)
from infrastructure.transports.browser_json import BrowserJsonTransport
from marketplace_maps_parser.cli_args import parse_args
from marketplace_maps_parser.runner import run_collection

URL = "https://www.ozon.ru/product/demo-123/"


class Page:
    async def close(self):
        pass


def test_auto_is_default_but_uses_only_invisible_playwright():
    args = parse_args(["--url", URL])
    assert args.transport == "playwright"
    assert args.fetch_strategy == "auto"
    assert not args.debug_dumps
    assert BrowserJsonTransport().fetch_strategy == "auto"


@pytest.mark.parametrize("payload", [
    {}, {"error": "unauthorized"}, {"layout": [], "widgetStates": {}},
    {"incidentId": "challenge", "challengeURL": "/challenge.html"},
])
def test_unrelated_api_layout_cannot_be_reported_as_zero_reviews(payload):
    with pytest.raises(RuntimeError):
        BrowserJsonTransport()._validate_reviews_payload(payload)


def test_explicit_empty_review_widget_is_valid():
    BrowserJsonTransport()._validate_reviews_payload({
        "layout": [], "widgetStates": {
            "webListReviews-1-default": '{"reviews":[]}',
        },
    })


def test_review_text_cannot_trigger_json_challenge_detection():
    body = json.dumps({"reviews": [{
        "reviewId": "r1", "text": "Включите JavaScript; challenge.html",
    }]})
    assert not BrowserJsonTransport._is_cloudflare_challenge(body)


@pytest.mark.asyncio
async def test_fast_fetch_reuses_document(monkeypatch):
    transport = BrowserJsonTransport()
    page = Page()
    calls = []

    async def fetch(**kwargs):
        calls.append(kwargs["internal_path"])
        assert kwargs["page"] is page
        return {"reviews": []}

    async def navigate(**kwargs):
        pytest.fail("Successful fetch must not reload the document")

    monkeypatch.setattr(transport, "_fetch_json_inside_page_via_fetch", fetch)
    monkeypatch.setattr(transport, "_fetch_json_via_navigation", navigate)
    for path in ("/reviews?page=1", "/reviews?page=2"):
        await transport._fetch_json_inside_page(page=page, internal_path=path)
    assert calls == ["/reviews?page=1", "/reviews?page=2"]


@pytest.mark.asyncio
async def test_auto_fallback_is_once_and_scoped_to_one_tab(monkeypatch):
    transport = BrowserJsonTransport()
    blocked, healthy = Page(), Page()
    fetch_calls = []
    nav_calls = []

    async def fetch(**kwargs):
        fetch_calls.append(kwargs["page"])
        if kwargs["page"] is blocked:
            raise RuntimeError("not JSON")
        return {"reviews": []}

    async def navigate(**kwargs):
        nav_calls.append(kwargs["page"])
        return {"reviews": []}

    monkeypatch.setattr(transport, "_fetch_json_inside_page_via_fetch", fetch)
    monkeypatch.setattr(transport, "_fetch_json_via_navigation", navigate)
    for page in (blocked, blocked, healthy):
        await transport._fetch_json_inside_page(page=page, internal_path="/p")
    assert fetch_calls == [blocked, healthy]
    assert nav_calls == [blocked, blocked]


@pytest.mark.asyncio
async def test_fetch_is_timed_and_keeps_browser_credentials():
    transport = BrowserJsonTransport(timeout_ms=1234)

    class FetchPage:
        async def evaluate(self, script, args):
            assert "AbortController" in script
            assert 'credentials: "include"' in script
            assert "clearTimeout(timer)" in script
            assert args["timeoutMs"] == 1234
            assert "/api/entrypoint-api.bx/page/json/v2?" in (
                args["endpointUrl"]
            )
            return {
                "status": 200, "url": args["endpointUrl"],
                "contentType": "application/json", "body": '{"reviews":[]}',
            }

    assert await transport._fetch_json_inside_page_via_fetch(
        page=FetchPage(), internal_path="/product/p-123/reviews?page=1",
    ) == {"reviews": []}


@pytest.mark.asyncio
async def test_http_200_challenge_is_not_a_successful_empty_response():
    class FetchPage:
        async def evaluate(self, *args):
            return {
                "status": 200, "url": "https://www.ozon.ru/api/test",
                "contentType": "application/json",
                "body": '{"incidentId":"blocked","challengeURL":"/challenge"}',
            }

    with pytest.raises(RuntimeError, match="challenge"):
        await BrowserJsonTransport()._fetch_json_inside_page_via_fetch(
            page=FetchPage(), internal_path="/p",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("failed"),
                                  asyncio.CancelledError()])
async def test_forced_fetch_propagates_failure(monkeypatch, error):
    transport = BrowserJsonTransport(fetch_strategy="fetch")

    async def fetch(**kwargs):
        raise error

    async def navigate(**kwargs):
        pytest.fail("Forced fetch/cancellation must not navigate")

    monkeypatch.setattr(transport, "_fetch_json_inside_page_via_fetch", fetch)
    monkeypatch.setattr(transport, "_fetch_json_via_navigation", navigate)
    with pytest.raises(type(error)):
        await transport._fetch_json_inside_page(
            page=Page(), internal_path="/p",
        )


@pytest.mark.asyncio
async def test_warmup_waits_for_content_through_context_replacement():
    transport = BrowserJsonTransport(settle_ms=90_000, timeout_ms=500)
    calls = 0
    disposed = False

    class Handle:
        async def dispose(self):
            nonlocal disposed
            disposed = True

    class ReadyPage:
        async def wait_for_function(self, expression, *, timeout):
            nonlocal calls
            calls += 1
            assert "data-review-uuid" in expression
            assert 0 < timeout <= 500
            if calls == 1:
                raise RuntimeError("Failed to find execution context")
            return Handle()

        async def wait_for_timeout(self, _):
            pytest.fail("Ready pages do not pay a fixed settle delay")

    await transport._settle_after_goto(ReadyPage())
    assert calls == 2
    assert disposed


@pytest.mark.asyncio
async def test_warmup_does_not_ignore_a_real_timeout():
    class TimeoutPage:
        async def wait_for_function(self, *args, **kwargs):
            raise TimeoutError("reviews never appeared")

    with pytest.raises(TimeoutError):
        await BrowserJsonTransport()._wait_for_reviews_ready(TimeoutPage())


@pytest.mark.asyncio
async def test_json_viewer_raw_node_avoids_extra_navigation():
    class ViewerPage:
        goto_calls = 0
        reads = 0

        async def goto(self, *args, **kwargs):
            self.goto_calls += 1
            return None

        async def evaluate(self, script):
            assert "window.JSONView?.json?.textContent" in script
            self.reads += 1
            # The raw Text node is incrementally filled after commit.
            return '{"reviews":' if self.reads == 1 else '{"reviews":[]}'

    page = ViewerPage()
    result = await BrowserJsonTransport()._fetch_json_via_navigation(
        page=page, internal_path="/p",
    )
    assert result == {"reviews": []}
    assert page.goto_calls == 1
    assert page.reads == 2


@pytest.mark.asyncio
async def test_challenge_blank_redirect_is_not_mistaken_for_success(
    monkeypatch,
):
    transport = BrowserJsonTransport()
    bodies = iter(["", '{"reviews":', '{"reviews":[]}'])

    async def body(_):
        return next(bodies)

    async def sleep(_):
        pass

    monkeypatch.setattr(transport, "_read_page_body", body)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await transport._wait_for_challenge_completion(
        page=SimpleNamespace(url="https://www.ozon.ru/api/test"),
        endpoint_url="https://www.ozon.ru/api/test",
        initial_body="enable JavaScript", initial_status=403,
        initial_url="https://www.ozon.ru/api/test",
        initial_content_type="text/html", max_wait_seconds=1,
    )
    assert json.loads(result[0]) == {"reviews": []}


@pytest.mark.asyncio
async def test_default_diagnostics_do_not_read_or_dump_dom(tmp_path):
    transport = BrowserJsonTransport(debug_dir=str(tmp_path / "debug"))
    await transport._save_debug(
        page=object(), payload={"reviews": []}, page_number=1,
    )
    assert not (tmp_path / "debug").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("page_count", [1, 3])
async def test_one_warmup_and_pacing_only_between_requested_pages(
    monkeypatch, page_count,
):
    from shared.pacing import AdaptivePacer

    transport = BrowserJsonTransport(page_delay_seconds=0.25)
    page = Page()
    gotos = []
    fetches = []
    waits = []

    @asynccontextmanager
    async def context():
        class Browser:
            async def new_page(self):
                return page
        yield Browser()

    async def goto(**kwargs):
        gotos.append(kwargs["reviews_url"])
        return page

    async def settle(_):
        pass

    async def fetch(**kwargs):
        fetches.append(kwargs["internal_path"])
        return {
            "reviews": [{"reviewId": str(len(fetches)), "rating": 5}],
            "nextPage": f"/product/p-123/reviews?page={len(fetches) + 1}",
        }

    async def wait(pacer):
        waits.append(pacer.base_delay)

    monkeypatch.setattr(transport, "_browser_context", context)
    monkeypatch.setattr(transport, "_goto_with_retry", goto)
    monkeypatch.setattr(transport, "_settle_after_goto", settle)
    monkeypatch.setattr(transport, "_fetch_json_with_retry", fetch)
    monkeypatch.setattr(AdaptivePacer, "wait", wait)
    pages = [p async for p in transport.iter_ozon_reviews_json(
        "/product/p-123", max_pages=page_count,
    )]
    assert len(pages) == page_count
    assert len(gotos) == 1
    assert len(fetches) == page_count
    assert waits == [0.25] * (page_count - 1)


def test_raw_node_count_matches_reviews_without_constructing_them(
    monkeypatch,
):
    uuid = "11111111-2222-3333-4444-555555555555"
    payload = {"widgetStates": {"webListReviews-1": json.dumps({
        "reviews": {uuid: {"rating": 5}},
        "duplicate": {"reviewId": uuid, "rating": 5},
    })}}

    def map_review(*args, **kwargs):
        pytest.fail("Counting nodes must not build full Review objects")

    monkeypatch.setattr(
        "infrastructure.marketplaces.ozon_payload.map_ozon_review_node",
        map_review,
    )
    nodes = BrowserJsonTransport._extract_review_nodes_from_payload(payload)
    assert len(nodes) == 1
    assert extract_review_id(nodes[0]) == uuid
    assert BrowserJsonTransport._review_node_id(nodes[0]) == uuid
    assert list(iter_ozon_review_nodes(payload)) == nodes


class ClosingTransport:
    def __init__(self):
        self.active = 0

    async def iter_ozon_reviews_json(self, **kwargs):
        self.active += 1
        try:
            for _ in range(10):
                yield 1, {"reviews": [
                    {"reviewId": str(i), "rating": 5} for i in range(600)
                ]}
        finally:
            await asyncio.sleep(0)
            self.active -= 1

    async def iter_ozon_reviews_by_scroll(self, **kwargs):
        self.active += 1
        try:
            yield [{"uuid": str(i), "rating": 5} for i in range(600)]
        finally:
            await asyncio.sleep(0)
            self.active -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,parallel", [
    ("scroll", False), ("pagination", False), ("pagination", True),
    ("auto", False), ("auto", True),
])
async def test_limit_closes_entire_nested_browser_chain_before_return(
    tmp_path: Path, strategy: str, parallel: bool,
):
    transport = ClosingTransport()
    adapter = OzonAdapter(transport)
    args = parse_args([
        "--url", URL, "--output", str(tmp_path / "reviews.json"),
        "--max-reviews", "1",
    ])
    await asyncio.wait_for(run_collection(
        args, adapter=adapter,
        make_iterator=lambda: adapter.iter_all_reviews(
            URL, strategy=strategy, parallel_streams=parallel,
        ),
    ), timeout=2)
    # Must not depend on gc.collect or loop.shutdown_asyncgens.
    assert transport.active == 0
    document = json.loads(Path(args.output).read_text())
    assert len(document["reviews"]) == 1
    assert document["diagnostics"]["error"] is None


@pytest.mark.asyncio
async def test_parallel_auto_falls_back_but_does_not_claim_completeness():
    class FailingTransport(ClosingTransport):
        async def iter_ozon_reviews_json(self, **kwargs):
            raise RuntimeError("API unavailable")
            yield  # pragma: no cover

    transport = FailingTransport()
    adapter = OzonAdapter(transport)
    reviews = [
        review async for review in adapter.iter_all_reviews(
            URL, parallel_streams=True, strategy="auto",
        )
    ]
    assert len(reviews) == 600
    assert transport.incomplete_reason == "api_stream_failure"
    assert transport.active == 0


@pytest.mark.asyncio
async def test_get_one_api_page_closes_browser(monkeypatch):
    transport = BrowserJsonTransport()
    closed = False

    @asynccontextmanager
    async def context():
        nonlocal closed
        try:
            yield SimpleNamespace(new_page=lambda: None)
        finally:
            closed = True

    async def pages(**kwargs) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        async with context():
            yield 1, {"reviews": []}

    monkeypatch.setattr(transport, "iter_ozon_reviews_json", pages)
    assert await transport.get_ozon_reviews_json("/p") == {"reviews": []}
    assert closed
