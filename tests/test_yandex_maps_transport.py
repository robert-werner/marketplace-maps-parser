"""Tests for the Yandex.Maps transport's pure helpers."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from infrastructure.transports.yandex_maps_browser import (
    aspect_chip_size,
    djb2_xor32,
    find_aspects,
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
