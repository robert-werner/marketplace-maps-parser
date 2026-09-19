"""Tests for the 2GIS adapter and transport state helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from infrastructure.marketplaces.two_gis import (
    TwoGisAdapter,
    parse_2gis_date,
)
from infrastructure.transports.two_gis_browser import (
    extract_2gis_reviews,
    find_entity_reviews_query,
)

FIRM_URL = "https://2gis.ru/moscow/firm/70000001063192616"

# Card shape measured 2026-09-19 from __REACT_QUERY_STATE__.
CARD = {
    "id": "143420181",
    "date_created": "2026-04-30T23:50:40.0Z",
    "rating": 1,
    "text": "Лор отделение шарашкина контора…",
    "user": {
        "id": "68350700",
        "name": "Ислам Багамаев",
    },
    "official_answer": {
        "date_created": "2025-03-05T17:28:38.163247+07:00",
        "id": "43142292",
        "org_name": (
            "Больница №67 им. Л.А. Ворохобова, хирургический корпус"
        ),
        "text": "Здравствуйте. Готовы разобраться…",
    },
    "provider": "2gis",
    "likes_count": 2,
    "media": [],
}

STATE = {
    "queries": [
        {
            "queryKey": ["vacanciesRegionList", {}],
            "state": {"data": {"result": {"items": []}}},
        },
        {
            "queryKey": [
                "fetchEntityReviews",
                ["70000001063192616", "branch", "trust"],
            ],
            "state": {
                "data": {
                    "pages": [
                        {
                            "items": [dict(CARD)],
                            "total": 22,
                            "rating": 3.6,
                            "orgRating": 4,
                            "hasMore": False,
                        },
                    ],
                },
            },
        },
    ],
}


class StubTransport:
    last_total_count: int | None = 22
    last_average_rating: float | None = 3.6
    last_product_title: str | None = (
        "Больница №67 им. Л.А. Ворохобова, хирургический корпус"
    )

    def __init__(
        self,
        batches: list[list[dict[str, Any]]],
    ) -> None:
        self.batches = batches

    async def iter_review_batches(
        self,
        firm_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        for batch in self.batches:
            yield batch


# --- transport state helpers ------------------------------------------------


def test_find_entity_reviews_query() -> None:
    query = find_entity_reviews_query(STATE)
    assert query is not None
    assert query["queryKey"][0] == "fetchEntityReviews"
    assert find_entity_reviews_query({"queries": []}) is None


def test_extract_2gis_reviews_cards_and_meta() -> None:
    cards, meta = extract_2gis_reviews(STATE)
    assert len(cards) == 1
    assert cards[0]["id"] == "143420181"
    assert meta["total"] == 22
    assert meta["rating"] == 3.6
    assert meta["hasMore"] is False


def test_extract_2gis_reviews_empty_state() -> None:
    assert extract_2gis_reviews({}) == ([], {})
    assert extract_2gis_reviews(None) == ([], {})


# --- adapter -----------------------------------------------------------------


async def test_2gis_review_happy_path() -> None:
    adapter = TwoGisAdapter(StubTransport([[dict(CARD)]]))
    reviews = [
        review
        async for review in adapter.iter_reviews(FIRM_URL)
    ]
    assert len(reviews) == 1
    review = reviews[0]
    assert review.review_id == "143420181"
    assert review.rating == 1
    assert review.author == "Ислам Багамаев"
    assert review.created_at == datetime(2026, 4, 30, 23, 50, 40)
    assert review.seller_answer == (
        "Здравствуйте. Готовы разобраться…"
    )
    assert review.product.marketplace == "2gis"
    assert review.product.product_id == "70000001063192616"
    assert review.raw["likes_count"] == 2


async def test_2gis_totals_propagate() -> None:
    adapter = TwoGisAdapter(StubTransport([[dict(CARD)]]))
    _ = [r async for r in adapter.iter_reviews(FIRM_URL)]
    assert adapter.last_total_count == 22
    assert adapter.last_average_rating == 3.6


async def test_2gis_dedup_by_id() -> None:
    adapter = TwoGisAdapter(StubTransport([
        [dict(CARD)],
        [dict(CARD)],
    ]))
    reviews = [
        r async for r in adapter.iter_reviews(FIRM_URL)
    ]
    assert len(reviews) == 1


async def test_2gis_collect_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        await TwoGisAdapter(StubTransport([])).collect(FIRM_URL)


def test_parse_2gis_date_variants() -> None:
    assert parse_2gis_date(
        "2026-04-30T23:50:40.0Z",
    ) == datetime(2026, 4, 30, 23, 50, 40)
    assert parse_2gis_date(
        "2025-03-05T17:28:38.163247+07:00",
    ) == datetime(2025, 3, 5, 17, 28, 38)
    assert parse_2gis_date("") is None
    assert parse_2gis_date(None) is None
    assert parse_2gis_date("не дата") is None


async def test_2gis_media_urls() -> None:
    card = dict(
        CARD,
        media=[
            {"url": "https://i.example/1.jpg"},
            {"preview_url": "https://i.example/2.jpg"},
            {"id": "no-url"},
        ],
    )
    adapter = TwoGisAdapter(StubTransport([[card]]))
    reviews = [
        r async for r in adapter.iter_reviews(FIRM_URL)
    ]
    assert reviews[0].photos == [
        "https://i.example/1.jpg",
        "https://i.example/2.jpg",
    ]
