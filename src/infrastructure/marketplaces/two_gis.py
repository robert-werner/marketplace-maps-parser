# src/infrastructure/marketplaces/two_gis.py
"""2GIS adapter: raw review cards → :class:`Review` entities.

The transport (``transports/two_gis_browser.py``) yields the SSR
cards from the ``fetchEntityReviews`` React-Query state. Measured
card shape (2026-09-19)::

    {
      "id": "143420181",
      "date_created": "2026-04-30T23:50:40.0Z",
      "rating": 1,
      "text": "…",
      "user": {"name": "Ислам Багамаев", …},
      "official_answer": {"text": "Здравствуйте. Готовы…",
                          "org_name": "…", …},
      "provider": "2gis", "likes_count": 2,
      "media": [], "emojis": …, "trust_factors": …,
    }

Mapping notes:

- ``official_answer`` is the org's reply → ``seller_answer``
  (2GIS is the first source in this project that exposes them);
- ``date_created`` is ISO-8601 with a variable-precision fraction
  and ``Z`` (``…T23:50:40.0Z``);
- rating-only «оценки» are separate objects with ``is_rated`` and
  no text — kept as-is; the histogram lives in the page meta, not
  the list.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol

from domain.entities import (
    ProductRef,
    Review,
    ReviewPage,
)
from infrastructure.marketplaces.base import MarketplaceAdapter
from infrastructure.marketplaces.yandex import (
    normalize_yandex_rating,
)
from shared.url_parsers import extract_2gis_branch_id

_ISO_PREFIX_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})",
)


class TwoGisTransportProtocol(Protocol):
    """The slice of the transport the adapter depends on."""

    last_total_count: int | None
    last_average_rating: float | None

    def iter_review_batches(
        self,
        firm_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]: ...


def parse_2gis_date(value: Any) -> datetime | None:
    """Parse an ISO-8601 ``date_created`` (``…T23:50:40.0Z``).

    ``datetime.fromisoformat`` chokes on the single-digit fraction
    on some versions — the regex route is uniform.
    """
    if not value:
        return None
    match = _ISO_PREFIX_RE.search(str(value))
    if not match:
        return None
    year, month, day, hour, minute, second = (
        int(part) for part in match.groups()
    )
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _media_url(item: Any) -> str | None:
    """Best-effort photo src from a media item."""
    if isinstance(item, str):
        return item or None
    if isinstance(item, dict):
        for key in ("url", "preview_url", "media_url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


class TwoGisAdapter(MarketplaceAdapter):
    """Maps raw 2GIS review cards to domain Reviews."""

    name = "2gis"

    def __init__(
        self,
        browser_transport: TwoGisTransportProtocol,
    ) -> None:
        self.transport = browser_transport
        #: Filled while iterating (the CLI prints them in the
        #: summary).
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None

    async def collect(self, firm_url: str) -> ReviewPage:
        """Not implemented: the 2GIS flow yields everything in one
        batch — use :meth:`iter_reviews` (streaming keeps the CLI
        wiring uniform)."""
        raise NotImplementedError(
            "TwoGisAdapter is streaming-only — use iter_reviews()"
        )

    async def iter_reviews(
        self,
        firm_url: str,
    ) -> AsyncIterator[Review]:
        """Stream deduplicated Reviews for one 2GIS firm."""
        branch_id = str(extract_2gis_branch_id(firm_url))
        product = ProductRef(
            marketplace=self.name,
            source_url=firm_url,
            product_id=branch_id,
        )

        seen: set[str] = set()
        async for batch in self.transport.iter_review_batches(
            firm_url,
        ):
            self.last_total_count = (
                self.transport.last_total_count
            )
            self.last_average_rating = (
                self.transport.last_average_rating
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
        user = card.get("user") or {}
        if not isinstance(user, dict):
            user = {}

        answer = card.get("official_answer") or {}
        if not isinstance(answer, dict):
            answer = {}

        photos = [
            url
            for url in (
                _media_url(item)
                for item in card.get("media") or []
            )
            if url
        ]

        review_id = card.get("id")
        review_id = str(review_id) if review_id else None

        return Review(
            review_id=review_id,
            product=product,
            rating=normalize_yandex_rating(card.get("rating")),
            text=self._clean_text(card.get("text")),
            author=self._clean_text(user.get("name")),
            created_at=parse_2gis_date(card.get("date_created")),
            seller_answer=self._clean_text(answer.get("text")),
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
    "TwoGisAdapter",
    "parse_2gis_date",
]
