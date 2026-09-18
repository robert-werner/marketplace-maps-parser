"""Tests for the Yandex.Market adapter."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from domain.entities import Review
from infrastructure.marketplaces.yandex import (
    YandexMarketAdapter,
    normalize_yandex_rating,
    parse_yandex_date,
    split_author_and_text,
)

URL = "https://market.yandex.ru/card/smartfon-x/12345678"


class StubTransport:
    """Yields canned batches; records the URL it was called with."""

    last_total_count: int | None = 47
    last_average_rating: float | None = 4.5

    def __init__(
        self,
        batches: list[list[dict[str, Any]]],
    ) -> None:
        self.batches = batches
        self.calls: list[str] = []

    async def iter_review_batches(
        self,
        product_url: str,
    ):
        self.calls.append(product_url)
        for batch in self.batches:
            yield batch


def _card(
    uuid: str | None,
    *,
    rating: Any = 5,
    text: str = "Отличный товар",
    author: str | None = "Иван",
    date: str = "5 октября 2023",
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "rating": rating,
        "text": text,
        "author": author,
        "date": date,
        "pros": "Качество",
        "cons": "Цена",
        "images": ["https://pics.example/1.jpg", ""],
    }


async def test_iter_reviews_maps_and_orders():
    transport = StubTransport(
        [
            [_card("y1"), _card("y2", rating=4)],
            [_card("y3", rating=3)],
        ],
    )
    adapter = YandexMarketAdapter(browser_transport=transport)

    reviews = [r async for r in adapter.iter_reviews(URL)]

    assert transport.calls == [URL]
    assert [r.review_id for r in reviews] == ["y1", "y2", "y3"]
    assert reviews[0].rating == 5
    assert reviews[1].rating == 4
    assert reviews[0].pros == "Качество"
    assert reviews[0].cons == "Цена"
    assert reviews[0].author == "Иван"
    assert reviews[0].created_at == datetime(2023, 10, 5)
    assert reviews[0].photos == ["https://pics.example/1.jpg"]
    assert reviews[0].product.marketplace == "yandex"
    assert reviews[0].product.product_id == "12345678"
    assert reviews[0].product.source_url == URL


async def test_iter_reviews_dedup_by_uuid_and_fallback():
    transport = StubTransport(
        [
            [_card("y1"), _card(None)],
            # same uuid again + an identical uuid-less card
            [_card("y1"), _card(None)],
        ],
    )
    adapter = YandexMarketAdapter(browser_transport=transport)

    reviews = [r async for r in adapter.iter_reviews(URL)]

    assert len(reviews) == 2


async def test_totals_propagated():
    transport = StubTransport([[_card("y1")]])
    adapter = YandexMarketAdapter(browser_transport=transport)

    assert adapter.last_total_count is None

    _ = [r async for r in adapter.iter_reviews(URL)]

    assert adapter.last_total_count == 47
    assert adapter.last_average_rating == 4.5


async def test_totals_survive_early_break():
    transport = StubTransport(
        [[_card("y1")], [_card("y2")]],
    )
    adapter = YandexMarketAdapter(browser_transport=transport)

    async for _review in adapter.iter_reviews(URL):
        break  # --max-reviews style early exit

    assert adapter.last_total_count == 47


async def test_author_split_from_merged_blob():
    transport = StubTransport(
        [
            [
                {
                    "uuid": "y1",
                    "rating": 5,
                    "text": "Иван Петров\nОтличный товар",
                },
            ],
        ],
    )
    adapter = YandexMarketAdapter(browser_transport=transport)

    reviews = [r async for r in adapter.iter_reviews(URL)]

    assert reviews[0].author == "Иван Петров"
    assert reviews[0].text == "Отличный товар"


async def test_bad_url_raises_value_error():
    adapter = YandexMarketAdapter(
        browser_transport=StubTransport([]),
    )

    with pytest.raises(ValueError):
        _ = [r async for r in adapter.iter_reviews(
            "https://ozon.ru/product/x-1",
        )]


async def test_collect_raises_not_implemented():
    adapter = YandexMarketAdapter(browser_transport=StubTransport([]))

    with pytest.raises(NotImplementedError):
        await adapter.collect(URL)


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        ("5", 5),
        (4.6, 5),
        (4.4, 4),
        ("5 из 5", None),  # float() fails -> None (JS pre-parses)
        (0, None),
        (9, None),
        (None, None),
        (True, None),
    ],
)
def test_normalize_yandex_rating(value, expected):
    assert normalize_yandex_rating(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5 октября 2023", datetime(2023, 10, 5)),
        ("15 января 2024, 10:30", datetime(2024, 1, 15)),
        ("2023-10-05", datetime(2023, 10, 5)),
        ("2023-10-05T10:00:00Z", datetime(2023, 10, 5)),
        ("05.10.2023", datetime(2023, 10, 5)),
        ("5 мартобря 2023", None),
        ("", None),
        (None, None),
        ("мусор", None),
    ],
)
def test_parse_yandex_date(value, expected):
    assert parse_yandex_date(value) == expected


def test_split_author_and_text():
    assert split_author_and_text("Иван\nТекст") == ("Иван", "Текст")
    assert split_author_and_text(None) == (None, None)
    assert split_author_and_text("") == (None, None)
    assert split_author_and_text("\n  \nТекст") == (None, "Текст")


async def test_review_entity_roundtrip():
    transport = StubTransport([[_card("y1")]])
    adapter = YandexMarketAdapter(browser_transport=transport)

    reviews = [r async for r in adapter.iter_reviews(URL)]

    assert isinstance(reviews[0], Review)
    assert reviews[0].raw["uuid"] == "y1"
