"""Tests for the page_key transition handling in
``BrowserJsonTransport.iter_ozon_reviews_json``.

These tests reproduce the production scenario the user reported:

    Page 6 (page_key=A): 30 reviews, nextPage=URL with page_key=B
    Page 7 (page_key=B): 0 reviews, nextPage=None  → BUG: stop here

After the fix, when a page_key transition is observed AND the new
page returns 0 reviews, the iterator retries with ``page=1`` and
``layout_page_index=1`` using the new page_key. This lets us continue
collecting reviews from the new variant instead of stopping short.

The tests stub out the heavy parts of ``BrowserJsonTransport`` (the
Playwright browser session and the inner JSON fetcher) so the
page_key detection logic can be exercised in pure Python.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from infrastructure.transports.browser_json.transport import (
    BrowserJsonTransport,
)

# ---------------------------------------------------------------------------
# Static helper tests (no async, no browser)
# ---------------------------------------------------------------------------


def test_extract_query_param_returns_value():
    path = (
        "/product/foo-123/reviews"
        "?page=7&page_key=CL21-abc&layout_page_index=7"
    )
    assert (
        BrowserJsonTransport._extract_query_param(path, "page_key")
        == "CL21-abc"
    )
    assert (
        BrowserJsonTransport._extract_query_param(path, "page")
        == "7"
    )


def test_extract_query_param_missing_returns_none():
    path = "/product/foo-123/reviews?page=1"
    assert (
        BrowserJsonTransport._extract_query_param(path, "page_key")
        is None
    )


def test_reset_page_in_path_replaces_page_and_layout():
    original = (
        "/product/foo-123/reviews"
        "?layout_container=default&layout_page_index=7"
        "&page=7&page_key=CL21-abc&reviewsVariantMode=2&sort="
    )
    reset = BrowserJsonTransport._reset_page_in_path(original, page=1)

    # page and layout_page_index must both be 1
    assert BrowserJsonTransport._extract_query_param(reset, "page") == "1"
    assert (
        BrowserJsonTransport._extract_query_param(reset, "layout_page_index")
        == "1"
    )
    # other params preserved
    assert (
        BrowserJsonTransport._extract_query_param(reset, "page_key")
        == "CL21-abc"
    )
    assert (
        BrowserJsonTransport._extract_query_param(reset, "reviewsVariantMode")
        == "2"
    )


def test_reset_page_in_path_adds_missing_params():
    """If page or layout_page_index are missing, they are added."""
    original = "/product/foo-123/reviews?page_key=CL21-abc"
    reset = BrowserJsonTransport._reset_page_in_path(original, page=1)
    assert BrowserJsonTransport._extract_query_param(reset, "page") == "1"
    assert (
        BrowserJsonTransport._extract_query_param(reset, "layout_page_index")
        == "1"
    )
    assert (
        BrowserJsonTransport._extract_query_param(reset, "page_key")
        == "CL21-abc"
    )


# ---------------------------------------------------------------------------
# Page_key transition handling inside iter_ozon_reviews_json
# ---------------------------------------------------------------------------


class _StubPage:
    """Just a placeholder; we monkeypatch the fetcher so the page
    object is never actually used."""

    async def goto(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, *args, **kwargs):
        return None

    async def content(self):
        return "<!-- stub -->"

    async def screenshot(self, *args, **kwargs):
        return None


async def _noop_sleep(*a, **kw):
    return None


def _make_transport():
    """Create a BrowserJsonTransport without spawning a browser."""
    return BrowserJsonTransport(
        timeout_ms=1,
        settle_ms=0,
        debug_dir="debug_ozon_test",
        humanize=False,
    )


def _reviews_payload(
    review_ids: list[str],
    *,
    next_path: str | None = None,
) -> dict[str, Any]:
    """Build an Ozon-style pagination payload."""
    payload: dict[str, Any] = {
        "_review_nodes": [
            {
                "reviewId": rid,
                "rating": 5,
                "text": f"review {rid}",
                "author": "tester",
            }
            for rid in review_ids
        ]
    }
    if next_path is not None:
        payload["nextPage"] = next_path
    else:
        payload["nextPage"] = None
    return payload


@pytest.mark.asyncio
async def test_page_key_transition_with_zero_reviews_triggers_retry(
    monkeypatch,
):
    """Reproduces the exact scenario the user reported:

    - Page 1 (page_key=A): 30 reviews, nextPage=URL(page=2, page_key=A)
    - Page 2 (page_key=A): 30 reviews,
      nextPage=URL(page=3, page_key=B)
      ← page_key transitions here
    - Page 3 (page_key=B, page=3): 0 reviews, nextPage=None  ← BUG previously

    After the fix, the iterator should retry with page=1 and the new
    page_key=B. The simulated fetcher will then return 30 fresh
    reviews for that reset URL, and the iterator continues.
    """
    transport = _make_transport()

    # URL constants. The first page never has a page_key (it's just
    # ``/product/foo-123/reviews?page=1``); page_key only appears in
    # Ozon's nextPage URLs starting from page 2.
    page1_url = "/product/foo-123/reviews?page=1"
    page2_url = (
        "/product/foo-123/reviews?page=2&page_key=AAAA1111&"
        "layout_page_index=2"
    )
    # Ozon gives us this as nextPage from page 2 — note new page_key
    page3_buggy_url = (
        "/product/foo-123/reviews?page=3&page_key=BBBB2222&"
        "layout_page_index=3"
    )
    # The reset URL our fix should construct
    page1_with_new_key = (
        "/product/foo-123/reviews?page=1&page_key=BBBB2222&"
        "layout_page_index=1"
    )
    page2_with_new_key = (
        "/product/foo-123/reviews?page=2&page_key=BBBB2222&"
        "layout_page_index=2"
    )

    # Map each internal_path to the payload it should return
    payloads_by_path: dict[str, dict[str, Any]] = {
        page1_url: _reviews_payload(
            [f"a{i}" for i in range(30)],
            next_path=page2_url,
        ),
        page2_url: _reviews_payload(
            [f"a{i}" for i in range(30, 60)],
            # nextPage points to page 3 with a new page_key
            next_path=page3_buggy_url,
        ),
        page3_buggy_url: _reviews_payload(
            [],  # 0 reviews — this is the bug
            next_path=None,
        ),
        # The reset attempt: 30 new reviews (variant B page 1)
        page1_with_new_key: _reviews_payload(
            [f"b{i}" for i in range(30)],
            next_path=page2_with_new_key,
        ),
        page2_with_new_key: _reviews_payload(
            [f"b{i}" for i in range(30, 60)],
            next_path=None,  # end of variant B
        ),
    }

    fetch_calls: list[str] = []

    async def fake_fetch_with_retry(
        self, *, page, internal_path, attempts=3, label="fetch",
    ):
        fetch_calls.append(internal_path)
        if internal_path not in payloads_by_path:
            raise RuntimeError(
                f"unexpected fetch: {internal_path}"
            )
        return payloads_by_path[internal_path]

    monkeypatch.setattr(
        BrowserJsonTransport,
        "_fetch_json_with_retry",
        fake_fetch_with_retry,
    )

    # Stub out the browser session so we never actually launch Chromium.
    class _FakeBrowser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def new_page(self):
            return _StubPage()

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: _FakeBrowser(),
    )
    # Stub _save_debug to avoid writing files.
    async def fake_save_debug(
        self, *, page, payload, page_number, stream_suffix=""
    ):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # Run the iterator and collect all payloads
    pages_yielded: list[tuple[int, dict[str, Any]]] = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        start_page=1,
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    # The iterator should have fetched:
    #   1. page1_url        (page 1, key A) — 30 reviews
    #   2. page2_url        (page 2, key A) — 30 reviews
    #   3. page3_buggy_url  (page 3, key B) — 0 reviews (trigger reset)
    #   4. page1_with_new_key (page 1, key B) — 30 reviews (the fix!)
    #   5. page2_with_new_key (page 2, key B) — 30 reviews
    assert fetch_calls == [
        page1_url,
        page2_url,
        page3_buggy_url,
        page1_with_new_key,
        page2_with_new_key,
    ], (
        f"Fetch sequence did not match expected. Got: {fetch_calls}"
    )

    # 5 pages yielded (the empty page3_buggy_url is NOT yielded
    # because we `continue` before yielding when the reset fires).
    assert len(pages_yielded) == 4

    # Total reviews across all yielded pages = 30*4 = 120
    total_reviews = sum(
        len(p.get("_review_nodes", []))
        for _, p in pages_yielded
    )
    assert total_reviews == 120


@pytest.mark.asyncio
async def test_page_key_transition_with_zero_reviews_retry_also_empty(
    monkeypatch,
):
    """If the retry with page=1 also returns 0 reviews, the iterator
    should stop without infinite-looping."""
    transport = _make_transport()

    page1_a = "/product/foo-123/reviews?page=1"
    page2_a = (
        "/product/foo-123/reviews?page=2&page_key=AAAA1111&"
        "layout_page_index=2"
    )
    page3_buggy_b = (
        "/product/foo-123/reviews?page=3&page_key=BBBB2222&"
        "layout_page_index=3"
    )
    page1_reset_b = (
        "/product/foo-123/reviews?page=1&page_key=BBBB2222&"
        "layout_page_index=1"
    )

    payloads_by_path: dict[str, dict[str, Any]] = {
        page1_a: _reviews_payload(
            [f"a{i}" for i in range(30)],
            next_path=page2_a,
        ),
        page2_a: _reviews_payload(
            [f"a{i}" for i in range(30, 60)],
            next_path=page3_buggy_b,
        ),
        page3_buggy_b: _reviews_payload([], next_path=None),
        # Reset attempt — also empty
        page1_reset_b: _reviews_payload([], next_path=None),
    }

    fetch_calls: list[str] = []

    async def fake_fetch(
        self, *, page, internal_path, attempts=3, label="fetch"
    ):
        fetch_calls.append(internal_path)
        return payloads_by_path[internal_path]

    monkeypatch.setattr(
        BrowserJsonTransport, "_fetch_json_with_retry", fake_fetch,
    )

    class _FakeBrowser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def new_page(self):
            return _StubPage()

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: _FakeBrowser(),
    )

    async def fake_save_debug(
        self, *, page, payload, page_number, stream_suffix=""
    ):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages_yielded: list[tuple[int, dict[str, Any]]] = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        start_page=1,
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    # Fetched: page1_a, page2_a, page3_buggy_b (triggers reset),
    # page1_reset_b (also empty → stop).
    assert fetch_calls == [
        page1_a,
        page2_a,
        page3_buggy_b,
        page1_reset_b,
    ]

    # The reset attempt (page1_reset_b) returned 0 reviews and is
    # yielded — but no further pages are fetched.
    # The buggy page3_buggy_b is NOT yielded (we `continue` past it).
    assert len(pages_yielded) == 3  # page1_a, page2_a, page1_reset_b

    # Total reviews = 30 + 30 + 0 = 60 (the reset didn't help here)
    total_reviews = sum(
        len(p.get("_review_nodes", []))
        for _, p in pages_yielded
    )
    assert total_reviews == 60


@pytest.mark.asyncio
async def test_no_page_key_in_url_no_reset_attempted(monkeypatch):
    """When the URL has no page_key at all (early pages, no variant
    yet), no reset should be attempted even if the page returns 0
    reviews."""
    transport = _make_transport()

    page1 = "/product/foo-123/reviews?page=1"
    page2 = "/product/foo-123/reviews?page=2"

    payloads_by_path: dict[str, dict[str, Any]] = {
        page1: _reviews_payload(
            [f"a{i}" for i in range(30)],
            next_path=page2,
        ),
        page2: _reviews_payload([], next_path=None),  # genuinely end
    }

    fetch_calls: list[str] = []

    async def fake_fetch(
        self, *, page, internal_path, attempts=3, label="fetch"
    ):
        fetch_calls.append(internal_path)
        return payloads_by_path[internal_path]

    monkeypatch.setattr(
        BrowserJsonTransport, "_fetch_json_with_retry", fake_fetch,
    )

    class _FakeBrowser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def new_page(self):
            return _StubPage()

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: _FakeBrowser(),
    )
    async def fake_save_debug(
        self, *, page, payload, page_number, stream_suffix=""
    ):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages_yielded: list[tuple[int, dict[str, Any]]] = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        start_page=1,
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    # No page_key, so no reset attempted — just 2 fetches.
    assert fetch_calls == [page1, page2]
    assert len(pages_yielded) == 2


@pytest.mark.asyncio
async def test_page_key_same_across_pages_no_reset_attempted(
    monkeypatch,
):
    """When page_key is present but stays the same across pages, no
    reset is attempted even if a page returns 0 reviews."""
    transport = _make_transport()

    # The first page has no page_key; the SAMEKEY is introduced starting
    # at page 2 and remains the same.
    page1 = "/product/foo-123/reviews?page=1"
    page2 = (
        "/product/foo-123/reviews?page=2&page_key=SAMEKEY&"
        "layout_page_index=2"
    )

    payloads_by_path: dict[str, dict[str, Any]] = {
        page1: _reviews_payload(
            [f"a{i}" for i in range(30)],
            next_path=page2,
        ),
        page2: _reviews_payload([], next_path=None),
    }

    fetch_calls: list[str] = []

    async def fake_fetch(
        self, *, page, internal_path, attempts=3, label="fetch"
    ):
        fetch_calls.append(internal_path)
        return payloads_by_path[internal_path]

    monkeypatch.setattr(
        BrowserJsonTransport, "_fetch_json_with_retry", fake_fetch,
    )

    class _FakeBrowser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def new_page(self):
            return _StubPage()

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: _FakeBrowser(),
    )
    async def fake_save_debug(
        self, *, page, payload, page_number, stream_suffix=""
    ):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages_yielded: list[tuple[int, dict[str, Any]]] = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        start_page=1,
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    # Same page_key, 0 reviews on page 2, nextPage=None → stop.
    # No reset attempt.
    assert fetch_calls == [page1, page2]
    assert len(pages_yielded) == 2


@pytest.mark.asyncio
async def test_nextpage_loop_guard_synthesizes_variant_b_pages(monkeypatch):
    """Reproduces the exact scenario from the user's log:

    - Page 1-6 with page_key=A: 30 reviews each, nextPage=page=N+1 + page_key=A
    - Page 6's nextPage points to page=7 with NEW page_key=B (bug trigger)
    - Page 7 (page_key=B, page=7): 0 reviews → triggers page_key reset
    - Reset URL: page=1 + page_key=B → returns 30 reviews
    - nextPage of the reset is page=2 + page_key=A (OLD key — Ozon points back)
    - WITHOUT the loop guard, this would loop forever (already seen) or stop
    - WITH the loop guard, we synthesize page=2 + page_key=B and continue
    - page=2 + page_key=B returns 30 reviews, nextPage=page=3 + page_key=B
    - ... etc until nextPage=None
    """
    transport = _make_transport()

    # URLs for variant A (already collected 6 pages before this run)
    page1_a = "/product/foo-123/reviews?page=1"
    page2_a = (
        "/product/foo-123/reviews?page=2&page_key=AAAA1111&"
        "layout_page_index=2"
    )
    # ... up to page 6, but for the test we only need page 6 to feed
    # the transition to variant B.
    page6_a = (
        "/product/foo-123/reviews?page=6&page_key=AAAA1111&"
        "layout_page_index=6"
    )
    # Page 7 with new page_key — 0 reviews, no nextPage (the bug trigger)
    page7_b_buggy = (
        "/product/foo-123/reviews?page=7&page_key=BBBB2222&"
        "layout_page_index=7"
    )
    # Reset URL — page 1 with new page_key
    page1_b = (
        "/product/foo-123/reviews?page=1&page_key=BBBB2222&"
        "layout_page_index=1"
    )
    # nextPage of page1_b — Ozon points back to variant A page 2 (OLD key)
    # We've already seen page2_a, so the loop guard should synthesize
    # page 2 with page_key=B instead.
    page2_b_synthesized = (
        "/product/foo-123/reviews?page=2&page_key=BBBB2222&"
        "layout_page_index=2"
    )
    # nextPage of page2_b (correctly points to page 3 with page_key=B)
    page3_b = (
        "/product/foo-123/reviews?page=3&page_key=BBBB2222&"
        "layout_page_index=3"
    )

    # Pre-seed seen_paths by simulating pages 1-6 of variant A.
    # For simplicity, we only set up pages 1, 2, 6 in this test, but
    # in the real flow the iterator visits all of them in order.
    payloads_by_path: dict[str, dict[str, Any]] = {
        # Variant A: 1, 2, then jump to 6 (skipping 3-5 for the test).
        page1_a: _reviews_payload(
            [f"a1-{i}" for i in range(30)],
            next_path=page2_a,
        ),
        page2_a: _reviews_payload(
            [f"a2-{i}" for i in range(30)],
            # For the test we shortcut to page 6 to keep the test short.
            next_path=page6_a,
        ),
        page6_a: _reviews_payload(
            [f"a6-{i}" for i in range(30)],
            # nextPage of page 6 has the NEW page_key but page=7 — bug trigger
            next_path=page7_b_buggy,
        ),
        page7_b_buggy: _reviews_payload([], next_path=None),
        # The reset — page 1 of variant B
        page1_b: _reviews_payload(
            [f"b1-{i}" for i in range(30)],
            # nextPage of page1_b points BACK to variant A page 2 (OLD key)
            next_path=page2_a,
        ),
        # The synthesized page 2 of variant B (created by our loop guard)
        page2_b_synthesized: _reviews_payload(
            [f"b2-{i}" for i in range(30)],
            # nextPage correctly points to page 3 with key B
            next_path=page3_b,
        ),
        page3_b: _reviews_payload(
            [f"b3-{i}" for i in range(30)],
            next_path=None,  # end of variant B
        ),
    }

    fetch_calls: list[str] = []

    async def fake_fetch(
        self, *, page, internal_path, attempts=3, label="fetch"
    ):
        fetch_calls.append(internal_path)
        if internal_path not in payloads_by_path:
            raise RuntimeError(f"unexpected fetch: {internal_path}")
        return payloads_by_path[internal_path]

    monkeypatch.setattr(
        BrowserJsonTransport, "_fetch_json_with_retry", fake_fetch,
    )

    class _FakeBrowser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def new_page(self):
            return _StubPage()

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: _FakeBrowser(),
    )
    async def fake_save_debug(
        self, *, page, payload, page_number, stream_suffix=""
    ):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages_yielded: list[tuple[int, dict[str, Any]]] = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        start_page=1,
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    # The fetch sequence MUST include the synthesized variant B page 2:
    #   1. page1_a         (variant A page 1)
    #   2. page2_a         (variant A page 2)
    #   3. page6_a         (variant A page 6)
    #   4. page7_b_buggy   (variant B page 7 — empty, triggers reset)
    #   5. page1_b         (variant B page 1 — reset succeeds)
    #   6. page2_b_synthesized  (synthesized page 2 with new key — LOOP GUARD)
    #   7. page3_b         (variant B page 3 — nextPage=None, stop)
    assert fetch_calls == [
        page1_a,
        page2_a,
        page6_a,
        page7_b_buggy,
        page1_b,
        page2_b_synthesized,
        page3_b,
    ], (
        f"Expected synthesized variant B page 2 in fetch sequence. "
        f"Got: {fetch_calls}"
    )

    # Total pages yielded = 6 (all except the empty page7_b_buggy)
    assert len(pages_yielded) == 6

    # Total reviews = 30 * 6 = 180
    total_reviews = sum(
        len(p.get("_review_nodes", []))
        for _, p in pages_yielded
    )
    assert total_reviews == 180


def test_map_node_pdp_reviews_shape():
    """pdp_reviews API nests the payload: node["content"] =
    {comment, score, positive, negative}. The mapper must unpack it
    (the whole-review dict must NOT leak into Review.text) and
    flatten the author object."""
    from domain.entities import ProductRef
    from infrastructure.marketplaces.ozon import map_ozon_review_node

    product = ProductRef(
        marketplace="ozon", source_url="u", product_id="1",
    )
    node = {
        "uuid": "u-1",
        "publishedAt": 1784804079,
        "author": {"firstName": "Алина И.", "lastName": ""},
        "content": {
            "comment": "Отличный наборчик",
            "score": 5,
            "positive": "работает",
            "negative": "дорогой",
        },
    }
    review = map_ozon_review_node(node=node, product=product)
    assert review is not None
    assert review.review_id == "u-1"
    assert review.text == "Отличный наборчик"
    assert review.rating == 5
    assert review.pros == "работает"
    assert review.cons == "дорогой"
    assert review.author == "Алина И."

    # отзыв только с оценкой (без комментария) — валиден
    node["content"] = {"comment": None, "score": 1}
    review = map_ozon_review_node(node=node, product=product)
    assert review is not None
    assert review.rating == 1
    assert review.text is None
