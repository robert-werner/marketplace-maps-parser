# src/infrastructure/marketplaces/yandex_maps.py
"""Yandex.Maps adapter: raw review dicts → :class:`Review` entities.

The transport (``transports/yandex_maps_browser.py``) streams
batches of raw review cards — the SAME shape from two sources: the
SSR ``state-view`` blob (page 1) and the intercepted
``/maps/api/business/fetchReviews`` XHR payloads (pages 2..N).
Measured card shape (2026-09-18)::

    {
      "reviewId": "mGuY8GHWiuqjF2Ui-…",
      "businessId": "1120018525",
      "author": {"name": "валентина иванова", …},
      "text": "Добрый,вежливый персонал…",
      "rating": 5,
      "updatedTime": "2021-12-25T12:35:46.523Z",
      "reactions": {"likes": 4, "dislikes": 2, …},
      "photos": [{"id": "urn:yandex:sprav:photo:…", …}],
      "videos": […], "pinned": false, …
    }

This module owns the mapping into the domain model (rating
normalisation and date parsing reuse the Yandex.Market helpers —
the sites share the Russian-date/ISO conventions).

Usage::

    adapter = YandexMapsAdapter(browser_transport=transport)
    async for review in adapter.iter_reviews(org_url):
        ...
    adapter.last_total_count      # site review counter
    adapter.last_average_rating   # org ratingValue
    adapter.last_rating_count     # assessments incl. rating-only
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from domain.entities import (
    ProductRef,
    Review,
    ReviewPage,
)
from infrastructure.marketplaces.base import MarketplaceAdapter
from infrastructure.marketplaces.yandex import (
    normalize_yandex_rating,
    parse_yandex_date,
)
from shared.url_parsers import extract_yandex_maps_org_id


class YandexMapsTransportProtocol(Protocol):
    """The slice of the transport the adapter depends on."""

    last_total_count: int | None
    last_average_rating: float | None
    last_rating_count: int | None

    def iter_review_batches(
        self,
        org_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]: ...


def _photo_url(photo: Any) -> str | None:
    """Best-effort photo src: a bare string or a dict with a
    url/template field (photo objects also carry id/tags only)."""
    if isinstance(photo, str):
        return photo or None
    if isinstance(photo, dict):
        for key in ("url", "urlTemplate", "src"):
            value = photo.get(key)
            if isinstance(value, str) and value:
                return value
    return None


class YandexMapsAdapter(MarketplaceAdapter):
    """Maps raw Yandex.Maps review cards to domain Reviews."""

    name = "yandex_maps"

    def __init__(
        self,
        browser_transport: YandexMapsTransportProtocol,
    ) -> None:
        self.transport = browser_transport
        #: Filled while iterating (the CLI prints them in the
        #: summary).
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        self.last_rating_count: int | None = None

    async def collect(self, org_url: str) -> ReviewPage:
        """Not implemented: the Maps flow is streaming-only (the
        reviews list lazy-loads page by page; there is no
        single-shot page)."""
        raise NotImplementedError(
            "YandexMapsAdapter is streaming-only — "
            "use iter_reviews()"
        )

    async def iter_reviews(
        self,
        org_url: str,
    ) -> AsyncIterator[Review]:
        """Stream deduplicated Reviews for one Maps organization.

        The transport already dedups across batches (by
        ``reviewId``); this is a second layer for adapter-level
        reuse — same pattern as the Yandex.Market adapter.
        """
        org_id = str(extract_yandex_maps_org_id(org_url))
        product = ProductRef(
            marketplace=self.name,
            source_url=org_url,
            product_id=org_id,
        )

        seen: set[str] = set()
        async for batch in self.transport.iter_review_batches(
            org_url,
        ):
            # Refresh the totals on every batch so they survive an
            # early break (the CLI reads them right after its loop).
            self.last_total_count = (
                self.transport.last_total_count
            )
            self.last_average_rating = (
                self.transport.last_average_rating
            )
            self.last_rating_count = (
                self.transport.last_rating_count
            )
            for card in batch:
                review = self._map_review(card, product)
                key = (
                    review.review_id
                    if review.review_id
                    else self._fallback_key(review)
                )
                if key in seen:
                    continue
                seen.add(key)
                yield review

    def _map_review(
        self,
        card: dict[str, Any],
        product: ProductRef,
    ) -> Review:
        author = card.get("author") or {}
        if not isinstance(author, dict):
            author = {}

        photos = [
            url
            for url in (
                _photo_url(photo)
                for photo in card.get("photos") or []
            )
            if url
        ]

        review_id = card.get("reviewId")
        review_id = str(review_id) if review_id else None

        return Review(
            review_id=review_id,
            product=product,
            rating=normalize_yandex_rating(
                card.get("rating"),
            ),
            text=self._clean_text(card.get("text")),
            author=self._clean_text(author.get("name")),
            created_at=parse_yandex_date(
                card.get("updatedTime"),
            ),
            photos=photos,
            raw=card,
        )

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _fallback_key(review: Review) -> str:
        return "|".join(
            (
                review.product.product_id,
                str(review.author or ""),
                str(review.created_at or ""),
                str(review.rating or ""),
                str(review.text or "")[:80],
            )
        )


__all__ = [
    "YandexMapsAdapter",
]
