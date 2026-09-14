"""Tests for the unified "collect ALL reviews" flow on OzonAdapter.

These tests stub out the browser transport entirely — no Playwright
required. They exercise:

- ``strategy="pagination"``  — only the pagination path
- ``strategy="scroll"``      — only the scroll path
- ``strategy="auto"``        — pagination first, scroll as fallback;
  cross-strategy deduplication by ``review_id``
- adapter-level fallback when the transport doesn't implement
  ``iter_all_ozon_reviews``
- ``max_reviews`` cap respected across both strategies
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from domain.entities import ProductRef, Review
from infrastructure.marketplaces.ozon import OzonAdapter


PRODUCT_URL = (
    "https://www.ozon.ru/product/"
    "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
)


# ---------------------------------------------------------------------------
# Test transports
# ---------------------------------------------------------------------------


class StubTransport:
    """In-memory transport implementing all three Ozon review iterators.

    Yields canned payloads so we can deterministically test the
    adapter's dedup / fallback / cap logic without a browser.
    """

    def __init__(
        self,
        *,
        pagination_payloads: list[dict[str, Any]] | None = None,
        scroll_batches: list[list[dict[str, Any]]] | None = None,
        pagination_error: Exception | None = None,
        scroll_error: Exception | None = None,
    ) -> None:
        self.pagination_payloads = pagination_payloads or []
        self.scroll_batches = scroll_batches or []
        self.pagination_error = pagination_error
        self.scroll_error = scroll_error
        self.iter_all_calls: list[dict[str, Any]] = []

    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        if self.pagination_error is not None:
            raise self.pagination_error
        for i, payload in enumerate(self.pagination_payloads):
            page_num = start_page + i
            if max_pages is not None and i >= max_pages:
                return
            yield page_num, payload

    async def get_ozon_reviews_json(
        self,
        product_path: str,
        *,
        page_number: int = 1,
    ) -> dict[str, Any]:
        if self.pagination_error is not None:
            raise self.pagination_error
        idx = page_number - 1
        if 0 <= idx < len(self.pagination_payloads):
            return self.pagination_payloads[idx]
        return {}

    async def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        if self.scroll_error is not None:
            raise self.scroll_error
        for batch in self.scroll_batches:
            yield batch

    async def iter_all_ozon_reviews(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
        pagination_max_pages: int | None = None,
        pagination_start_page: int = 1,
        scroll_max_rounds: int = 500,
        page_delay_seconds: float = 1.5,
        scroll_pause_seconds: float = 1.0,
        retry_attempts: int = 3,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.iter_all_calls.append(
            {
                "max_reviews": max_reviews,
                "pagination_max_pages": pagination_max_pages,
                "pagination_start_page": pagination_start_page,
            }
        )
        seen: set[str] = set()

        # --- pagination phase ---
        for i, payload in enumerate(self.pagination_payloads):
            if (
                pagination_max_pages is not None
                and i >= pagination_max_pages
            ):
                break
            for node in payload.get("_review_nodes", []):
                rid = node.get("reviewId") or node.get("uuid")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                yield "pagination", node
                if (
                    max_reviews is not None
                    and len(seen) >= max_reviews
                ):
                    return

        # --- scroll phase ---
        for batch in self.scroll_batches:
            for card in batch:
                rid = card.get("uuid")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                yield "scroll", card
                if (
                    max_reviews is not None
                    and len(seen) >= max_reviews
                ):
                    return


# ---------------------------------------------------------------------------
# Helpers to build review-shaped payloads
# ---------------------------------------------------------------------------


def _pagination_payload(
    review_ids: list[str],
) -> dict[str, Any]:
    """Build a pagination-style payload with N reviews."""
    return {
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


def _scroll_card(uuid: str) -> dict[str, Any]:
    """Build a DOM-style scroll card."""
    return {
        "uuid": uuid,
        "text": f"line0\n{uuid}\n2024-01-01\nreview text",
        "published_at": "1704067200",
    }


# ---------------------------------------------------------------------------
# Tests — strategy: pagination only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_reviews_pagination_only() -> None:
    transport = StubTransport(
        pagination_payloads=[
            _pagination_payload(["p1", "p2", "p3"]),
            _pagination_payload(["p4", "p5"]),
        ],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
        )
    ]

    assert [r.review_id for r in reviews] == [
        "p1", "p2", "p3", "p4", "p5",
    ]
    # iter_all_ozon_reviews must NOT be called in pagination-only mode
    assert transport.iter_all_calls == []


# ---------------------------------------------------------------------------
# Tests — strategy: scroll only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_reviews_scroll_only() -> None:
    transport = StubTransport(
        scroll_batches=[
            [_scroll_card("s1"), _scroll_card("s2")],
            [_scroll_card("s3")],
        ],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="scroll",
        )
    ]

    assert [r.review_id for r in reviews] == ["s1", "s2", "s3"]
    assert transport.iter_all_calls == []


# ---------------------------------------------------------------------------
# Tests — strategy: auto (transport supports iter_all_ozon_reviews)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_reviews_auto_pagination_and_scroll() -> None:
    """auto strategy = pagination first, then scroll as supplement."""
    transport = StubTransport(
        pagination_payloads=[
            _pagination_payload(["p1", "p2"]),
        ],
        scroll_batches=[
            [_scroll_card("s1"), _scroll_card("s2")],
        ],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="auto",
        )
    ]

    ids = [r.review_id for r in reviews]
    assert ids == ["p1", "p2", "s1", "s2"]
    assert transport.iter_all_calls == [
        {
            "max_reviews": None,
            "pagination_max_pages": None,
            "pagination_start_page": 1,
        }
    ]


@pytest.mark.asyncio
async def test_iter_all_reviews_auto_cross_strategy_dedup() -> None:
    """If the same UUID appears in both pagination and scroll, it
    should only be yielded once."""
    shared_uuid = "11111111-2222-3333-4444-555555555555"
    transport = StubTransport(
        pagination_payloads=[
            _pagination_payload([shared_uuid, "p2"]),
        ],
        scroll_batches=[
            [_scroll_card(shared_uuid), _scroll_card("s2")],
        ],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="auto",
        )
    ]

    ids = [r.review_id for r in reviews]
    # shared_uuid appears once (from pagination, since pagination runs first)
    assert ids == [shared_uuid, "p2", "s2"]


@pytest.mark.asyncio
async def test_iter_all_reviews_auto_max_reviews_cap() -> None:
    transport = StubTransport(
        pagination_payloads=[
            _pagination_payload(["p1", "p2", "p3", "p4"]),
        ],
        scroll_batches=[
            [_scroll_card("s1"), _scroll_card("s2")],
        ],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="auto",
            max_reviews=3,
        )
    ]

    assert len(reviews) == 3
    assert [r.review_id for r in reviews] == ["p1", "p2", "p3"]


# ---------------------------------------------------------------------------
# Tests — adapter-level fallback (transport without iter_all_ozon_reviews)
# ---------------------------------------------------------------------------


class StubTransportWithoutIterAll:
    """Transport implementing only the legacy two iterators."""

    def __init__(
        self,
        *,
        pagination_payloads: list[dict[str, Any]] | None = None,
        scroll_batches: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.pagination_payloads = pagination_payloads or []
        self.scroll_batches = scroll_batches or []

    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        for i, payload in enumerate(self.pagination_payloads):
            if max_pages is not None and i >= max_pages:
                return
            yield start_page + i, payload

    async def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        for batch in self.scroll_batches:
            yield batch


@pytest.mark.asyncio
async def test_iter_all_reviews_adapter_fallback() -> None:
    """If the transport doesn't implement iter_all_ozon_reviews, the
    adapter falls back to running pagination + scroll itself."""
    shared_uuid = "11111111-2222-3333-4444-555555555555"
    transport = StubTransportWithoutIterAll(
        pagination_payloads=[
            _pagination_payload([shared_uuid, "p2"]),
        ],
        scroll_batches=[
            [_scroll_card(shared_uuid), _scroll_card("s2")],
        ],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="auto",
        )
    ]

    ids = [r.review_id for r in reviews]
    assert ids == [shared_uuid, "p2", "s2"]


@pytest.mark.asyncio
async def test_iter_all_reviews_adapter_fallback_pagination_only_error() -> None:
    """If pagination raises, the fallback should still try scroll."""
    transport = StubTransportWithoutIterAll(
        pagination_payloads=[],
        scroll_batches=[[_scroll_card("s1"), _scroll_card("s2")]],
    )

    # Wrap iter_ozon_reviews_json to raise
    original = transport.iter_ozon_reviews_json

    async def raising_iter(*args, **kwargs):
        raise RuntimeError("pagination exploded")
        yield  # pragma: no cover

    transport.iter_ozon_reviews_json = raising_iter  # type: ignore

    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="auto",
        )
    ]

    # Adapter fallback logs the pagination failure but still produces
    # scroll reviews.
    assert [r.review_id for r in reviews] == ["s1", "s2"]


# ---------------------------------------------------------------------------
# Tests — invalid strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_reviews_invalid_strategy_raises() -> None:
    transport = StubTransport()
    adapter = OzonAdapter(browser_transport=transport)

    with pytest.raises(ValueError, match="Unknown strategy"):
        async for _ in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="bogus",
        ):
            pass  # pragma: no cover
