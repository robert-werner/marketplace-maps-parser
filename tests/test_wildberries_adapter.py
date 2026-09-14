"""Tests for the Wildberries adapter.

Uses a stub transport that returns canned JSON — no network required.
"""
from __future__ import annotations

from typing import Any

import pytest

from domain.entities import ReviewPage
from infrastructure.marketplaces.wildberries import (
    WildberriesAdapter,
)
from shared.url_parsers import extract_nm_id


class StubWildberriesTransport:
    """Returns canned card + feedback responses based on the URL."""

    def __init__(
        self,
        card_payload: dict[str, Any],
        feedbacks_payload: dict[str, Any],
    ) -> None:
        self.card_payload = card_payload
        self.feedbacks_payload = feedbacks_payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((url, params))
        if "card.wb.ru" in url:
            return self.card_payload
        if "feedbacks1.wb.ru" in url:
            return self.feedbacks_payload
        raise AssertionError(f"unexpected URL: {url}")


WB_URL = (
    "https://www.wildberries.ru/catalog/12345678/detail.aspx"
)


def _card_payload() -> dict[str, Any]:
    return {
        "products": [
            {"root": 999, "name": "Test product"},
        ]
    }


def _feedbacks_payload() -> dict[str, Any]:
    return {
        "feedbackCount": 2,
        "valuation": 4.5,
        "feedbacks": [
            {
                "id": 111,
                "productValuation": 5,
                "text": "Отлично",
                "pros": "Качество",
                "cons": None,
                "userName": "Иван",
                "createdDate": "2024-01-01T00:00:00Z",
                "answer": {"text": "Спасибо"},
            },
            {
                "id": 222,
                "productValuation": 4,
                "text": "Нормально",
                "pros": None,
                "cons": "Цена",
                "userName": "Мария",
                "createdDate": "2024-02-01T00:00:00Z",
                "answer": None,
            },
        ],
    }


@pytest.mark.asyncio
async def test_wildberries_adapter_collect() -> None:
    transport = StubWildberriesTransport(
        card_payload=_card_payload(),
        feedbacks_payload=_feedbacks_payload(),
    )
    adapter = WildberriesAdapter(transport)

    page = await adapter.collect(WB_URL)

    assert isinstance(page, ReviewPage)
    assert page.product.product_id == "12345678"
    assert page.product.parent_id == "999"
    assert page.total_count == 2
    assert page.average_rating == 4.5

    assert len(page.reviews) == 2
    r1, r2 = page.reviews

    assert r1.review_id == "111"
    assert r1.rating == 5
    assert r1.text == "Отлично"
    assert r1.pros == "Качество"
    assert r1.cons is None
    assert r1.author == "Иван"
    assert r1.seller_answer == "Спасибо"

    assert r2.review_id == "222"
    assert r2.rating == 4
    assert r2.cons == "Цена"
    assert r2.seller_answer is None


@pytest.mark.asyncio
async def test_wildberries_adapter_missing_product() -> None:
    transport = StubWildberriesTransport(
        card_payload={"products": []},
        feedbacks_payload=_feedbacks_payload(),
    )
    adapter = WildberriesAdapter(transport)

    with pytest.raises(ValueError, match="не найден"):
        await adapter.collect(WB_URL)


@pytest.mark.asyncio
async def test_wildberries_adapter_missing_root() -> None:
    transport = StubWildberriesTransport(
        card_payload={
            "products": [{"name": "no root here"}]
        },
        feedbacks_payload=_feedbacks_payload(),
    )
    adapter = WildberriesAdapter(transport)

    with pytest.raises(ValueError, match="root"):
        await adapter.collect(WB_URL)


def test_extract_nm_id_used_by_adapter() -> None:
    """Sanity: the URL parser and the adapter agree on the SKU."""
    assert extract_nm_id(WB_URL) == 12345678
