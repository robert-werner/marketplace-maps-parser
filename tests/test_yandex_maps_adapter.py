"""Tests for the Yandex.Maps adapter: raw cards → Review."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from domain.entities import Review
from infrastructure.marketplaces.yandex_maps import (
    YandexMapsAdapter,
)

ORG_URL = (
    "https://yandex.ru/maps/org/"
    "otdeleniye_pochtovoy_svyazi_430028/1120018525"
)

# Card shape measured 2026-09-18 from fetchReviews / state-view.
CARD = {
    "reviewId": "mGuY8GHWiuqjF2Ui-uG4ItR6crO34Gi6_",
    "businessId": "1120018525",
    "author": {
        "name": "валентина иванова",
        "publicId": "urhjv4brem49r30pjkvbfa0jxm",
    },
    "text": "Добрый,вежливый персонал.",
    "rating": 5,
    "updatedTime": "2021-12-25T12:35:46.523Z",
    "reactions": {"likes": 4, "dislikes": 2},
    "photos": [],
    "videos": [],
    "pinned": False,
}


class StubTransport:
    """Yields canned batches (the transport protocol slice)."""

    last_total_count: int | None = 86
    last_average_rating: float | None = 4.0
    last_rating_count: int | None = 358

    def __init__(
        self,
        batches: list[list[dict[str, Any]]],
    ) -> None:
        self.batches = batches

    async def iter_review_batches(
        self,
        org_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        for batch in self.batches:
            yield batch


async def _collect_all(
    adapter: YandexMapsAdapter,
    org_url: str = ORG_URL,
) -> list[Review]:
    return [
        review async for review in adapter.iter_reviews(org_url)
    ]


async def test_maps_review_happy_path() -> None:
    reviews = await _collect_all(
        YandexMapsAdapter(StubTransport([[dict(CARD)]])),
    )
    assert len(reviews) == 1
    review = reviews[0]
    assert review.review_id == CARD["reviewId"]
    assert review.rating == 5
    assert review.text == "Добрый,вежливый персонал."
    assert review.author == "валентина иванова"
    assert review.created_at == datetime(2021, 12, 25)
    assert review.product.marketplace == "yandex_maps"
    assert review.product.product_id == "1120018525"
    assert review.raw["reactions"]["likes"] == 4


async def test_maps_photos_shapes() -> None:
    card = dict(
        CARD,
        photos=[
            "https://example.com/a.jpg",
            {"url": "https://example.com/b.jpg"},
            {"urlTemplate": "https://example.com/c/{size}"},
            {"id": "urn:yandex:sprav:photo:x"},
        ],
    )
    reviews = await _collect_all(
        YandexMapsAdapter(StubTransport([[card]])),
    )
    assert reviews[0].photos == [
        "https://example.com/a.jpg",
        "https://example.com/b.jpg",
        "https://example.com/c/{size}",
    ]


async def test_maps_rating_normalisation() -> None:
    reviews = await _collect_all(
        YandexMapsAdapter(StubTransport([[dict(CARD, rating=4.6)]])),
    )
    assert reviews[0].rating == 5

    reviews = await _collect_all(
        YandexMapsAdapter(StubTransport([[dict(CARD, rating=None)]])),
    )
    assert reviews[0].rating is None


async def test_maps_dedup_by_review_id() -> None:
    adapter = YandexMapsAdapter(StubTransport([
        [dict(CARD)],
        [dict(CARD), dict(CARD, reviewId="another-id")],
    ]))
    reviews = await _collect_all(adapter)
    assert [r.review_id for r in reviews] == [
        CARD["reviewId"],
        "another-id",
    ]


async def test_maps_dedup_fallback_key_without_id() -> None:
    card = dict(CARD)
    del card["reviewId"]
    adapter = YandexMapsAdapter(StubTransport([
        [dict(card)],
        [dict(card)],
    ]))
    reviews = await _collect_all(adapter)
    assert len(reviews) == 1


async def test_maps_totals_propagate() -> None:
    adapter = YandexMapsAdapter(StubTransport([[dict(CARD)]]))
    await _collect_all(adapter)
    assert adapter.last_total_count == 86
    assert adapter.last_average_rating == 4.0
    assert adapter.last_rating_count == 358


async def test_maps_collect_not_implemented() -> None:
    adapter = YandexMapsAdapter(StubTransport([]))
    with pytest.raises(NotImplementedError):
        await adapter.collect(ORG_URL)


async def test_maps_rejects_non_maps_url() -> None:
    adapter = YandexMapsAdapter(StubTransport([]))
    with pytest.raises(ValueError):
        await _collect_all(
            adapter,
            "https://market.yandex.ru/card/foo/123",
        )


async def test_maps_author_missing() -> None:
    reviews = await _collect_all(
        YandexMapsAdapter(StubTransport([[dict(CARD, author={})]])),
    )
    assert reviews[0].author is None
