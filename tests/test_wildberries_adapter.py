"""Wildberries live-DOM adapter tests without network calls."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from domain.entities import ReviewPage
from infrastructure.marketplaces.wildberries import (
    WildberriesAdapter,
)
from shared.url_parsers import extract_nm_id

WB_URL = 'https://www.wildberries.ru/catalog/12345678/detail.aspx'


class StubWildberriesTransport:
    last_total_count = 3
    last_average_rating = 4.5
    last_product_title = 'Test product'

    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = batches
        self.urls: list[str] = []

    async def iter_review_batches(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        self.urls.append(product_url)
        for batch in self.batches:
            yield batch


def _cards() -> list[dict[str, Any]]:
    return [
        {
            'author': 'Иван', 'date': '01 января 2024',
            'rating': 5, 'text': None,
            'sections': [
                {'label': 'Достоинства:', 'value': 'Качество'},
                {'label': 'Комментарий:', 'value': 'Отлично'},
            ],
            'answer': 'Спасибо', 'photos': ['https://example.org/a.webp'],
        },
        {
            'author': 'Мария', 'date': '02 февраля 2024',
            'rating': 4, 'text': 'Нормально',
            'sections': [], 'answer': None, 'photos': [],
        },
    ]


@pytest.mark.asyncio
async def test_wildberries_adapter_collect() -> None:
    cards = _cards()
    transport = StubWildberriesTransport([cards, cards])
    adapter = WildberriesAdapter(transport)

    page = await adapter.collect(WB_URL)

    assert isinstance(page, ReviewPage)
    assert page.product.product_id == '12345678'
    assert page.total_count == 3
    assert page.average_rating == 4.5
    assert adapter.last_product_title == 'Test product'
    assert transport.urls == [WB_URL]
    assert len(page.reviews) == 2
    r1, r2 = page.reviews
    assert r1.review_id == WildberriesAdapter._map_review(
        cards[0], page.product,
    ).review_id
    assert r1.rating == 5
    assert r1.text == 'Отлично'
    assert r1.pros == 'Качество'
    assert r1.created_at and r1.created_at.year == 2024
    assert r1.photos == ['https://example.org/a.webp']
    assert r1.seller_answer == 'Спасибо'
    assert r1.raw['id'] == r1.review_id
    assert r2.rating == 4
    assert r2.text == 'Нормально'
    assert r2.seller_answer is None


@pytest.mark.asyncio
async def test_wildberries_empty_batches() -> None:
    page = await WildberriesAdapter(
        StubWildberriesTransport([[]]),
    ).collect(WB_URL)
    assert page.reviews == []


def test_wildberries_date_and_id() -> None:
    assert extract_nm_id(WB_URL) == 12345678
    assert WildberriesAdapter._parse_date('Нет даты') is None
    assert WildberriesAdapter._parse_date('31 февраля 2024') is None
    assert WildberriesAdapter._parse_date(
        'Сегодня, 12:16 · Дополнен'
    ).date() == datetime.now().date()
    assert WildberriesAdapter._parse_date(
        'Вчера, 10:00'
    ).date() == (datetime.now() - timedelta(days=1)).date()
