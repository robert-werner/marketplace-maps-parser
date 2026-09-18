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

from collections.abc import AsyncIterator
from typing import Any

import pytest

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
        # Payloads served for non-default streams (extra_query
        # set, e.g. "&sort=score_asc").
        variant_payloads: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pagination_payloads = pagination_payloads or []
        self.scroll_batches = scroll_batches or []
        self.pagination_error = pagination_error
        self.scroll_error = scroll_error
        self.variant_payloads = variant_payloads or []
        self.received_extra_queries: list[str] = []
        self.iter_all_calls: list[dict[str, Any]] = []

    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
        retry_attempts: int = 3,
        extra_query: str = "",
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        self.received_extra_queries.append(extra_query)
        if self.pagination_error is not None:
            raise self.pagination_error
        payloads = (
            self.pagination_payloads
            if not extra_query
            else self.variant_payloads
        )
        for i, payload in enumerate(payloads):
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
        retry_attempts: int = 3,
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
async def test_iter_all_reviews_adapter_fallback_pagination_error() -> None:
    """If pagination raises, the fallback should still try scroll."""
    transport = StubTransportWithoutIterAll(
        pagination_payloads=[],
        scroll_batches=[[_scroll_card("s1"), _scroll_card("s2")]],
    )

    # Make pagination raise before the fallback scroll pass.
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


# ---------------------------------------------------------------------------
# Tests — parallel streams (pagination strategy, --parallel-streams)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_streams_merges_and_dedups() -> None:
    """All streams run concurrently; the merge loop dedups centrally."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(["d1", "d2"])],
        variant_payloads=[_pagination_payload(["d2", "a1", "a2"])],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=True,
        )
    ]

    ids = {r.review_id for r in reviews}
    # score_asc and score_desc both serve variant_payloads here;
    # the union must dedup across all three streams.
    assert ids == {"d1", "d2", "a1", "a2"}
    assert sorted(transport.received_extra_queries) == [
        "",
        "&sort=score_asc",
        "&sort=score_desc",
    ]


@pytest.mark.asyncio
async def test_parallel_streams_survive_one_failing_stream() -> None:
    """A stream that raises is logged and skipped; the others keep
    going and their reviews are still yielded."""

    class _FailingAscTransport(StubTransport):
        async def iter_ozon_reviews_json(
            self,
            product_path: str,
            *,
            start_page: int = 1,
            max_pages: int | None = None,
            retry_attempts: int = 3,
            extra_query: str = "",
        ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
            if extra_query == "&sort=score_asc":
                raise RuntimeError("asc stream died")
            async for item in super().iter_ozon_reviews_json(
                product_path=product_path,
                start_page=start_page,
                max_pages=max_pages,
                extra_query=extra_query,
            ):
                yield item

    transport = _FailingAscTransport(
        pagination_payloads=[_pagination_payload(["d1"])],
        variant_payloads=[_pagination_payload(["s1"])],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=True,
        )
    ]

    # default + score_desc survived; score_asc failure is swallowed.
    assert {r.review_id for r in reviews} == {"d1", "s1"}


@pytest.mark.asyncio
async def test_parallel_streams_respects_max_reviews_cap() -> None:
    """Reaching max_reviews cancels the remaining workers (no hang)
    and the total yielded count is capped."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(["d1", "d2", "d3"])],
        variant_payloads=[_pagination_payload(["a1", "a2", "a3"])],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=True,
            max_reviews=2,
        )
    ]

    assert len(reviews) == 2
    assert len({r.review_id for r in reviews}) == 2


@pytest.mark.asyncio
async def test_parallel_streams_single_stream_degrades_gracefully() -> None:
    """extra_streams=False → one worker; the concurrent path still
    works and yields the stream's reviews."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(["p1", "p2"])],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=False,
            parallel_streams=True,
        )
    ]

    assert [r.review_id for r in reviews] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# Tests — duplicate-streak early stop and filter streams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_streams_dup_streak_stops_re_serving() -> None:
    """A stream whose reviews are ALL already claimed by other
    streams stops itself after dup_streak_stop consecutive
    duplicates — reviews behind the duplicate stretch are not
    walked (bounded waste), here x1/x2 stay uncollected."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(
            ["d1", "d2", "d3", "d4", "d5"],
        )],
        variant_payloads=[_pagination_payload(
            ["d1", "d2", "d3", "x1", "x2"],
        )],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=True,
            dup_streak_stop=3,
        )
    ]

    # default claims d1..d5; asc/desc hit 3 consecutive dups and
    # stop before reaching x1/x2.
    assert {r.review_id for r in reviews} == {
        "d1", "d2", "d3", "d4", "d5",
    }


@pytest.mark.asyncio
async def test_sequential_dup_streak_stops_stream() -> None:
    """Same protection in the sequential mode: the stream after the
    default one stops after the duplicate streak."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(
            ["d1", "d2", "d3", "d4", "d5"],
        )],
        variant_payloads=[_pagination_payload(
            ["d1", "d2", "d3", "x1", "x2"],
        )],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=False,
            dup_streak_stop=3,
        )
    ]

    assert {r.review_id for r in reviews} == {
        "d1", "d2", "d3", "d4", "d5",
    }


@pytest.mark.asyncio
async def test_dup_streak_zero_disables_early_stop() -> None:
    """dup_streak_stop=0 → streams walk their full windows even
    through long duplicate stretches."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(
            ["d1", "d2", "d3"],
        )],
        variant_payloads=[_pagination_payload(
            ["d1", "d2", "d3", "x1", "x2"],
        )],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=True,
            dup_streak_stop=0,
        )
    ]

    # With the stop disabled, asc/desc walk past the duplicates and
    # reach x1/x2.
    assert {r.review_id for r in reviews} == {
        "d1", "d2", "d3", "x1", "x2",
    }


@pytest.mark.asyncio
async def test_filter_streams_add_with_photos_and_media() -> None:
    """filter_streams=True adds the withPhotos/withMedia windows to
    the stream list."""
    transport = StubTransport(
        pagination_payloads=[_pagination_payload(["d1"])],
        variant_payloads=[_pagination_payload(["f1"])],
    )
    adapter = OzonAdapter(browser_transport=transport)

    reviews = [
        r
        async for r in adapter.iter_all_reviews(
            product_url=PRODUCT_URL,
            strategy="pagination",
            extra_streams=True,
            parallel_streams=True,
            filter_streams=True,
        )
    ]

    assert {r.review_id for r in reviews} == {"d1", "f1"}
    assert "&withPhotos=true" in transport.received_extra_queries
    assert "&withMedia=true" in transport.received_extra_queries
