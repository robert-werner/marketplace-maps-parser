# src/infrastructure/marketplaces/wildberries.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from shared.url_parsers import extract_nm_id


class WildberriesHttpTransport(Protocol):
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = ...,
        headers: dict[str, str] | None = ...,
    ) -> dict[str, Any]: ...


class WildberriesAdapter(MarketplaceAdapter):
    name = "wildberries"

    def __init__(
        self,
        transport: WildberriesHttpTransport,
    ) -> None:
        self.transport = transport
        #: Product title for the unified output's ``product_title``
        #: (brand + name from the card detail API).
        self.last_product_title: str | None = None

    async def collect(self, product_url: str) -> ReviewPage:
        nm_id = extract_nm_id(product_url)

        card = await self.transport.get_json(
            "https://card.wb.ru/cards/v4/detail",
            params={
                "appType": 1,
                "curr": "rub",
                "dest": -1257786,
                "nm": nm_id,
            },
        )

        products = card.get("products") or []
        if not products:
            raise ValueError(f"Товар WB не найден: {product_url}")

        product = products[0]
        imt_id = product.get("root")

        if not imt_id:
            raise ValueError(f"У WB отсутствует root: {product_url}")

        self.last_product_title = (
            " ".join(
                part
                for part in (
                    product.get("brand"),
                    product.get("name"),
                )
                if isinstance(part, str) and part.strip()
            )
            or None
        )

        raw = await self.transport.get_json(
            f"https://feedbacks1.wb.ru/feedbacks/v1/{imt_id}",
        )

        product_ref = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(nm_id),
            parent_id=str(imt_id),
        )

        return ReviewPage(
            product=product_ref,
            reviews=[
                self._map_review(item, product_ref)
                for item in raw.get("feedbacks", [])
            ],
            total_count=raw.get("feedbackCount"),
            average_rating=raw.get("valuation"),
            raw=raw,
        )

    def _map_review(
        self,
        item: dict[str, Any],
        product: ProductRef,
    ) -> Review:
        answer = item.get("answer") or {}

        return Review(
            review_id=str(
                item.get("id")
                or item.get("feedbackId")
                or ""
            ) or None,
            product=product,
            rating=item.get("productValuation"),
            text=item.get("text"),
            pros=item.get("pros"),
            cons=item.get("cons"),
            author=item.get("userName"),
            created_at=self._parse_date(item.get("createdDate")),
            seller_answer=answer.get("text"),
            raw=item,
        )

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None

        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00"),
            )
        except ValueError:
            return None