"""Tests for the Ozon payload walker and review node mapper.

These tests use synthetic payloads that exercise the fuzzy matcher
(`is_review_node`) and the recursive walker (`walk_json`). They do NOT
require a running browser.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from domain.entities import ProductRef
from infrastructure.marketplaces.ozon import (
    build_review_key,
    extract_ozon_rating_summary,
    extract_reviews_from_ozon_payload,
    map_ozon_review_node,
    normalize_rating,
    normalize_text,
    parse_ozon_date,
    walk_json,
)

PRODUCT = ProductRef(
    marketplace="ozon",
    source_url=(
        "https://www.ozon.ru/product/"
        "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    ),
    product_id="680123890",
)


def test_walk_json_descends_into_lists() -> None:
    payload = {
        "items": [
            {"reviewId": "r1", "text": "ok", "rating": 5},
            {"reviewId": "r2", "text": "bad", "rating": 1},
        ]
    }
    nodes = [n for n in walk_json(payload) if isinstance(n, dict)]
    # root + items list wrapper dropped, but the two review dicts must be there
    ids = {n.get("reviewId") for n in nodes}
    assert {"r1", "r2"}.issubset(ids)


def test_walk_json_recurses_into_embedded_json_strings() -> None:
    """A JSON-encoded string inside the payload must be walked too."""
    payload = {
        "widget": (
            '{"reviewId": "r3", "text": "embedded", "rating": 4}'
        )
    }
    nodes = [n for n in walk_json(payload) if isinstance(n, dict)]
    ids = {n.get("reviewId") for n in nodes}
    assert "r3" in ids


def test_map_ozon_review_node_complete() -> None:
    node = {
        "reviewId": "abc123",
        "rating": 5,
        "text": "Отличный телефон",
        "pros": "Звук",
        "cons": "Цена",
        "author": "Иван",
        "createdAt": "2024-03-15T10:30:00Z",
    }
    review = map_ozon_review_node(node, PRODUCT)
    assert review is not None
    assert review.review_id == "abc123"
    assert review.rating == 5
    assert review.text == "Отличный телефон"
    assert review.pros == "Звук"
    assert review.cons == "Цена"
    assert review.author == "Иван"
    assert review.created_at == datetime(
        2024, 3, 15, 10, 30, tzinfo=UTC
    )


def test_map_ozon_review_node_rejects_non_review() -> None:
    """A dict without review-id-ish keys is NOT a review."""
    node = {"foo": "bar", "baz": 42}
    review = map_ozon_review_node(node, PRODUCT)
    assert review is None


def test_map_ozon_review_node_accepts_uuid_as_key() -> None:
    """When the dict key (stored as _ozon_key) is a UUID, treat it as id."""
    node = {
        "_ozon_key": "11111111-2222-3333-4444-555555555555",
        "text": "ok",
        "rating": 5,
    }
    review = map_ozon_review_node(node, PRODUCT)
    assert review is not None
    assert (
        review.review_id
        == "11111111-2222-3333-4444-555555555555"
    )


def test_extract_reviews_dedupes_by_review_id() -> None:
    payload = {
        "reviews": [
            {"reviewId": "r1", "text": "a", "rating": 5},
            {"reviewId": "r1", "text": "a", "rating": 5},
            {"reviewId": "r2", "text": "b", "rating": 4},
        ]
    }
    reviews = extract_reviews_from_ozon_payload(payload, PRODUCT)
    assert len(reviews) == 2
    assert {r.review_id for r in reviews} == {"r1", "r2"}


def test_extract_reviews_fallback_composite_key() -> None:
    """Reviews with same review_id are deduped; differing position-based
    composite keys are kept separately."""
    payload = {
        "reviews": [
            {
                "_ozon_key": (
                    "11111111-2222-3333-4444-555555555555"
                ),
                "author": "X",
                "rating": 5,
                "text": "a",
            },
            {
                "_ozon_key": (
                    "11111111-2222-3333-4444-555555555555"
                ),
                "author": "X",
                "rating": 5,
                "text": "a",
            },
            {
                "_ozon_key": (
                    "22222222-3333-4444-5555-666666666666"
                ),
                "author": "Y",
                "rating": 4,
                "text": "b",
            },
        ]
    }
    reviews = extract_reviews_from_ozon_payload(payload, PRODUCT)
    # Two unique UUIDs → two unique reviews.
    assert len(reviews) == 2


# --- helpers ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        (5, 5),
        (5.0, 5),
        (4.5, 4.5),
        ("5", 5),
        ("4,5", 4.5),
        (None, None),
        (True, None),  # bool is rejected
        ("not-a-number", None),
        ("", None),
    ],
)
def test_normalize_rating(raw: Any, expected: Any) -> None:
    assert normalize_rating(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  hello  ", "hello"),
        ("", None),
        (None, None),
        (42, "42"),
    ],
)
def test_normalize_text(raw: Any, expected: Any) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "raw, expected_iso",
    [
        (
            "2024-03-15T10:30:00Z",
            "2024-03-15T10:30:00+00:00",
        ),
        (
            "2024-03-15T10:30:00+03:00",
            "2024-03-15T10:30:00+03:00",
        ),
        ("15.03.2024", "2024-03-15T00:00:00+00:00"),
        # NOTE: "2024-03-15" is parsed by fromisoformat as a NAIVE
        # datetime (no tz) — production behavior. Do not expect UTC.
        ("2024-03-15", "2024-03-15T00:00:00"),
        (None, None),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_parse_ozon_date(
    raw: Any, expected_iso: str | None
) -> None:
    result = parse_ozon_date(raw)
    if result is None:
        assert expected_iso is None
    else:
        assert result.isoformat() == expected_iso


def test_parse_ozon_date_handles_unix_ms() -> None:
    # 2024-01-01 00:00:00 UTC in milliseconds
    ts_ms = 1_704_067_200_000
    result = parse_ozon_date(ts_ms)
    assert result == datetime(2024, 1, 1, tzinfo=UTC)


def test_parse_ozon_date_handles_unix_timestamp_string() -> None:
    """PublicPageTransport reads ``publishedat`` from the DOM."""
    result = parse_ozon_date("1704067200")
    assert result == datetime(2024, 1, 1, tzinfo=UTC)


def test_build_review_key_is_stable() -> None:
    """build_review_key is used as a dedup fallback; it must be stable."""
    # is_review_node requires a non-None review_id (via extract_review_id),
    # so we provide one via the UUID key fallback path.
    review = map_ozon_review_node(
        {
            "_ozon_key": (
                "11111111-2222-3333-4444-555555555555"
            ),
            "text": "hi",
            "rating": 5,
        },
        PRODUCT,
    )
    assert review is not None
    key1 = build_review_key(review, page_number=2, position=3)
    key2 = build_review_key(review, page_number=2, position=3)
    assert key1 == key2
    # Different position → different key
    key3 = build_review_key(review, page_number=2, position=4)
    assert key1 != key3


def _score_widget_payload() -> dict[str, Any]:
    """A pdp_reviews-style payload with a webReviewProductScore
    widget state (the widget value is a JSON-encoded string, exactly
    like the real API returns)."""
    widget = {
        "score": [
            {"title": "5 звёзд", "value": 4093},
            {"title": "4 звезды", "value": 199},
            {"title": "3 звезды", "value": 53},
            {"title": "2 звезды", "value": 31},
            {"title": "1 звезда", "value": 96},
        ],
        "reviewsCount": 4472,
        "totalScore": 4.8,
    }
    return {
        "widgetStates": {
            "webReviewProductScore-14003865-default-1": (
                json.dumps(widget, ensure_ascii=False)
            ),
            "webListReviews-5603940-default-1": "{}",
        }
    }


def test_extract_ozon_rating_summary_parses_histogram() -> None:
    summary = extract_ozon_rating_summary(
        _score_widget_payload(),
        product_id="2879817631",
        product_url="https://www.ozon.ru/product/foo",
    )
    assert summary is not None
    assert summary["histogram"] == {
        "5": 4093,
        "4": 199,
        "3": 53,
        "2": 31,
        "1": 96,
    }
    assert summary["reviews_count"] == 4472
    assert summary["average_score"] == 4.8
    assert summary["product_id"] == "2879817631"


def test_extract_ozon_rating_summary_returns_none_without_widget() -> None:
    """Payloads without widgetStates (e.g. DOM-card payloads from
    the public_page transport) must yield None, not an error."""
    assert extract_ozon_rating_summary({"reviews": []}) is None
    assert extract_ozon_rating_summary(
        {"widgetStates": {"webListReviews-1-default-1": "{}"}},
    ) is None
