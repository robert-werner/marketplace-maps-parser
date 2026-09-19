"""Tests for the Yandex.Maps transport's pure helpers."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from infrastructure.transports.yandex_maps_browser import (
    YandexMapsBrowserTransport,
    _DirectApiUnavailable,
    aspect_chip_size,
    build_api_streams,
    djb2_xor32,
    find_aspects,
    find_org_name,
    find_rating_data,
    find_review_results,
    sign_maps_query,
)

# Trimmed state-view shape (measured 2026-09-18): the reviews sit
# deep inside the serialized org object.
STATE: dict[str, Any] = {
    "config": {"requestId": "…", "csrfToken": "…"},
    "organization": {
        "name": "Отделение почтовой связи № 430028",
        "ratingData": {
            "ratingCount": 358,
            "ratingValue": 4,
            "reviewCount": 86,
        },
        "reviewResults": {
            "reviews": [
                {
                    "reviewId": "mGuY8GHWiuqjF2Ui-…",
                    "rating": 5,
                    "text": "…",
                },
                {"reviewId": "second", "rating": 1},
            ],
            "params": {
                "offset": 0,
                "limit": 50,
                "count": 86,
                "page": 1,
                "totalPages": 2,
                "reviewsRemained": 36,
            },
        },
    },
}


def test_find_review_results_locates_nested_reviews() -> None:
    results = find_review_results(STATE)
    assert results is not None
    assert len(results["reviews"]) == 2
    assert results["params"]["count"] == 86


def test_find_review_results_walks_lists() -> None:
    results = find_review_results([STATE["organization"]])
    assert results is not None
    assert results["params"]["totalPages"] == 2


def test_find_review_results_none_when_absent() -> None:
    assert find_review_results({"data": {"reviews": []}}) is None
    assert find_review_results(None) is None


def test_find_rating_data_locates_nested_aggregate() -> None:
    rating_data = find_rating_data(STATE)
    assert rating_data == {
        "ratingCount": 358,
        "ratingValue": 4,
        "reviewCount": 86,
    }


def test_find_rating_data_none_when_absent() -> None:
    assert find_rating_data({"organization": {}}) is None


def test_find_org_name_title_or_name() -> None:
    # Measured on the post office: the org object keys its title
    # as ``title``; other builds use ``name`` / ``shortTitle``.
    state_title = {
        "organization": {
            "title": "Отделение почтовой связи № 430028",
            "ratingData": {"ratingCount": 358},
        },
    }
    assert find_org_name(state_title) == (
        "Отделение почтовой связи № 430028"
    )

    state_name = {
        "organization": {
            "name": "ГКБ 67",
            "ratingData": {"ratingCount": 7852},
        },
    }
    assert find_org_name(state_name) == "ГКБ 67"
    assert find_org_name({"data": {"ratingData": {}}}) is None


# --- aspect chip labels ----------------------------------------------------

def test_aspect_chip_size_parses_counts() -> None:
    assert aspect_chip_size(
        "Персонал · 66%положительный3184 отзыва",
    ) == 3184
    assert aspect_chip_size("Справки · 13%отрицательный17 отзывов") == (
        17
    )
    assert aspect_chip_size("Реабилитация · 83%положительный1 отзыв") == 1


def test_aspect_chip_size_zero_without_count() -> None:
    assert aspect_chip_size("Персонал") == 0
    assert aspect_chip_size("") == 0


# --- the reversed s-signature ----------------------------------------------
#
# Live captured requests (2026-09-19): the query minus ``s`` must
# re-produce the exact ``s`` the site sent.

_SIGNED_ORACLES = [
    # Live captured requests (2026-09-19, org 1132076225): the
    # query minus ``s`` must re-produce the exact ``s`` the site
    # sent.
    (
        "ajax=1&businessId=1132076225&csrfToken=2cc818add7ea1c0b8c4"
        "e922b8a25f9db307728fd%3A1789797447&locale=ru_RU&page=2&pag"
        "eSize=50&ranking=by_relevance_org&reqId=1789797447470167-2"
        "259772038-addrs-upper-yp-76&s=657707922&sessionId=17897974"
        "47435624-2145213963534995513-balancer-l7leveler-kubr-yp-vl"
        "a-89-BAL",
        657707922,
    ),
    (
        "ajax=1&businessId=1132076225&csrfToken=2cc818add7ea1c0b8c4"
        "e922b8a25f9db307728fd%3A1789797447&locale=ru_RU&page=10&pa"
        "geSize=50&ranking=by_rating_asc&reqId=1789797447470167-22"
        "59772038-addrs-upper-yp-76&s=3971465772&sessionId=17897974"
        "47435624-2145213963534995513-balancer-l7leveler-kubr-yp-vl"
        "a-89-BAL",
        3971465772,
    ),
]


def _roundtrip_check(query: str, expected_s: int) -> None:
    params = dict(parse_qsl(query, keep_blank_values=True))
    signed = sign_maps_query(
        {k: v for k, v in params.items() if k != "s"},
    )
    rebuilt = dict(parse_qsl(signed, keep_blank_values=True))
    assert int(rebuilt["s"]) == expected_s
    assert {k: v for k, v in rebuilt.items() if k != "s"} == {
        k: v for k, v in params.items() if k != "s"
    }


def test_sign_maps_query_reproduces_captured_s() -> None:
    for query, expected_s in _SIGNED_ORACLES:
        _roundtrip_check(query, expected_s)


def test_djb2_xor32_known_vectors() -> None:
    # Cross-checked against the JS implementation in the base
    # chunk (h = 33*h ^ charCode, >>> 0).
    assert djb2_xor32("") == 5381
    assert djb2_xor32("a") == (33 * 5381) ^ ord("a")
    # 32-bit wraparound behaves like JS >>> 0.
    long_input = "x" * 10_000
    assert 0 <= djb2_xor32(long_input) < 2**32


def test_find_aspects_extracts_ids_and_counts() -> None:
    state: dict[str, Any] = {
        "organization": {
            "aspects": [
                {"id": "3502044050", "text": "Персонал", "count": 3184},
                {"id": "3502126758", "text": "Обслуживание", "count": 70},
            ],
        },
    }
    aspects = find_aspects(state)
    assert [a["id"] for a in aspects] == [
        "3502044050", "3502126758",
    ]
    assert find_aspects({"data": {}}) == []


# --- the direct-API stream matrix -------------------------------------------

_GLOBALS = [
    ("by_relevance_org", None),
    ("by_time", None),
    ("by_rating_desc", None),
    ("by_rating_asc", None),
]
_ASPECT_RANKS = [
    "by_relevance_org",
    "by_time",
    "by_rating_desc",
    "by_rating_asc",
    "by_aspect_tone_desc",
    "by_aspect_tone_asc",
]


def test_build_api_streams_globals_only_without_extra() -> None:
    assert build_api_streams(
        [{"id": "1", "count": 1000}],
        walk_extra_streams=False,
        dup_streak_stop=300,
    ) == _GLOBALS


def test_build_api_streams_expands_aspect_six_rankings() -> None:
    streams = build_api_streams(
        [{"id": "7", "count": 1000}],
        walk_extra_streams=True,
        dup_streak_stop=300,
    )
    assert streams[:4] == _GLOBALS
    assert streams[4:] == [(r, "7") for r in _ASPECT_RANKS]


def test_build_api_streams_small_aspect_collapses_on_full_drain() -> None:
    # ≤600-review aspects fit one window: a full drain walks a
    # single ranking for them; the dup-guard mode still expands.
    full_drain = build_api_streams(
        [{"id": "7", "count": 100}],
        walk_extra_streams=True,
        dup_streak_stop=0,
    )
    assert ("by_relevance_org", "7") in full_drain
    assert ("by_time", "7") not in full_drain
    guarded = build_api_streams(
        [{"id": "7", "count": 100}],
        walk_extra_streams=True,
        dup_streak_stop=300,
    )
    assert ("by_time", "7") in guarded


# --- the direct-API walk against a fake page --------------------------------
#
# The fake serves canned fetchReviews payloads keyed by
# (ranking, aspectId, page) and echoes the requested pageSize in
# params.limit — the contract the pageSize probe relies on.

_TEMPLATE_URL = (
    "https://yandex.ru/maps/api/business/fetchReviews"
    "?ajax=1&businessId=1&csrfToken=tok&locale=ru_RU"
    "&pageSize=50&reqId=req&sessionId=sess"
)


class _FakeResponse:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeMapsPage:
    """`page.evaluate` stand-in: parses the signed URL, serves the
    canned payload (missing keys → the past-the-window «Internal
    error»). `honor_page_size=False` simulates a server that clamps
    pageSize back to 50."""

    def __init__(
        self,
        pages: dict[tuple[str, str | None, int], dict],
        *,
        honor_page_size: bool = True,
    ) -> None:
        self._pages = pages
        self._honor = honor_page_size
        self.calls: list[dict[str, str]] = []

    async def evaluate(
        self, script: str, url: str | None = None,
    ) -> dict[str, Any]:
        assert url is not None
        query = dict(parse_qsl(urlsplit(url).query))
        self.calls.append(query)
        key = (
            query["ranking"],
            query.get("aspectId"),
            int(query["page"]),
        )
        payload = dict(
            self._pages.get(key)
            or {"error": {"message": "Internal error"}},
        )
        data = payload.get("data")
        if isinstance(data, dict):
            params = dict(data.get("params") or {})
            params["limit"] = (
                int(query.get("pageSize", "50"))
                if self._honor
                else 50
            )
            data["params"] = params
        return {"status": 200, "payload": payload}


def _card(review_id: str) -> dict[str, Any]:
    return {"reviewId": review_id, "rating": 5}


def _drain(
    transport: YandexMapsBrowserTransport,
    page: _FakeMapsPage,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        async for batch in transport._iter_direct_api(
            page, [_FakeResponse(_TEMPLATE_URL)], set(), state,
        ):
            cards.extend(batch)
        return cards

    return asyncio.run(run())


def _transport(**kwargs: Any) -> YandexMapsBrowserTransport:
    defaults: dict[str, Any] = {
        "api_pacing_seconds": 0.0,
        "api_concurrency": 2,
        "debug_dir": "debug_test_yandex_maps",
    }
    defaults.update(kwargs)
    return YandexMapsBrowserTransport(**defaults)


_PAGES: dict[tuple[str, str | None, int], dict] = {
    ("by_relevance_org", None, 1): {
        "data": {
            "reviews": [_card("r1"), _card("r2")],
            "params": {"count": 4},
        },
    },
    ("by_relevance_org", None, 2): {
        "data": {
            "reviews": [_card("r3"), _card("r4")],
            "params": {"count": 4},
        },
    },
    # by_time re-serves known ground on page 1, errors on page 2.
    ("by_time", None, 1): {
        "data": {
            "reviews": [_card("r1"), _card("r2")],
            "params": {"count": 4},
        },
    },
}


def test_iter_direct_api_collects_union_and_stops_at_total() -> None:
    transport = _transport()
    page = _FakeMapsPage(_PAGES)
    cards = _drain(transport, page, STATE)
    assert sorted(c["reviewId"] for c in cards) == [
        "r1", "r2", "r3", "r4",
    ]
    assert transport.last_total_count == 4


def test_iter_direct_api_page_size_probe_accepted() -> None:
    transport = _transport()
    page = _FakeMapsPage(_PAGES)
    _drain(transport, page, STATE)
    assert page.calls  # probe + stream pages all went through
    assert all(
        c.get("pageSize") == "100" for c in page.calls
    )


def test_iter_direct_api_page_size_probe_rejected_reverts() -> None:
    transport = _transport()
    page = _FakeMapsPage(_PAGES, honor_page_size=False)
    _drain(transport, page, STATE)
    probe, *rest = page.calls
    assert probe.get("pageSize") == "100"  # the probe was attempted
    assert all(c.get("pageSize") == "50" for c in rest)


def test_iter_direct_api_validation_error_propagates() -> None:
    validation_pages = {
        key: {"error": {"message": "Validation failed: bad param"}}
        for key in _PAGES
    }
    transport = _transport()
    page = _FakeMapsPage(validation_pages)
    with pytest.raises(_DirectApiUnavailable):
        _drain(transport, page, STATE)


def test_iter_direct_api_probe_page_reviews_not_wasted() -> None:
    transport = _transport()
    page = _FakeMapsPage(_PAGES)
    cards = _drain(transport, page, STATE)
    ids = [c["reviewId"] for c in cards]
    # The probe IS page 1 of the first stream: its cards arrive
    # exactly once despite the stream re-fetching the page.
    assert ids.count("r1") == 1
    assert ids.count("r2") == 1
