"""Normalize Avito seller/profile reviews (not product reviews)."""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from infrastructure.marketplaces.wildberries import WildberriesAdapter
from shared.async_iterators import closing_iterator
from shared.url_parsers import extract_avito_profile_id


def section_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, list):
        return None
    return "\n".join(
        row["text"].strip() for row in value
        if isinstance(row, dict) and isinstance(row.get("text"), str)
        and row["text"].strip()
    ) or None


def avito_card_key(card: dict[str, Any]) -> str:
    if card.get("id") is not None:
        return str(card["id"])
    identity = "\x1f".join(str(v or "") for v in (
        card.get("title"), card.get("rated"), card.get("score"),
        section_text(card.get("textSections")),
    ))
    return hashlib.sha256(identity.encode()).hexdigest()


class AvitoAdapter(MarketplaceAdapter):
    name = "avito"

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.last_product_title: str | None = None
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None

    async def collect(self, product_url: str) -> ReviewPage:
        reviews = [r async for r in self.iter_reviews(product_url)]
        return ReviewPage(
            ProductRef(self.name, product_url, extract_avito_profile_id(
                product_url,
            )),
            reviews, total_count=self.last_total_count,
        )

    async def iter_reviews(self, url: str) -> AsyncIterator[Review]:
        product = ProductRef(self.name, url, extract_avito_profile_id(url))
        seen: set[str] = set()
        async with closing_iterator(
            self.transport.iter_review_batches(url),
        ) as stream:
            async for cards in stream:
                self.last_product_title = self.transport.last_product_title
                self.last_total_count = self.transport.last_total_count
                self.last_average_rating = self.transport.last_average_rating
                for card in cards:
                    key = avito_card_key(card)
                    if key in seen:
                        continue
                    seen.add(key)
                    answer = card.get("answer") or {}
                    images = card.get("images") or []
                    photos: list[str] = []
                    for item in images:
                        if not isinstance(item, dict):
                            continue
                        photo = (
                            item.get("url")
                            or item.get("original")
                            or item.get("1280x960")
                            or item.get("640x480")
                        )
                        if isinstance(photo, str):
                            photos.append(photo)
                    yield Review(
                        review_id=key, product=product,
                        rating=card.get("score"),
                        text=section_text(card.get("textSections")),
                        author=card.get("title"),
                        created_at=WildberriesAdapter._parse_date(
                            str(card.get("rated") or ""),
                        ),
                        seller_answer=section_text(
                            answer.get("textSections")
                            if isinstance(answer, dict) else answer,
                        ),
                        photos=photos,
                        raw=card,
                    )
