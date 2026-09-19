# src/infrastructure/marketplaces/yandex.py
"""Yandex.Market adapter: raw card dicts → :class:`Review` entities.

The transport (``transports/yandex_browser.py``) streams batches of
raw review-card dicts scraped from the public reviews page. This
module owns the mapping into the domain model:

- stable ``review_id`` from the card's ``uuid`` (composite
  ``author|date|rating|text`` fallback when the DOM gives none);
- rating normalisation to int 1..5;
- Russian date parsing (``5 октября 2023`` / iso-like variants);
- best-effort author / date / text splitting when the card returns
  one merged blob (the DOM does not always label fields).

Usage::

    adapter = YandexMarketAdapter(browser_transport=transport)
    async for review in adapter.iter_reviews(product_url):
        ...
    adapter.last_total_count  # review counter from the page, if any
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
from shared.url_parsers import extract_yandex_market_product_id

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

_RU_DATE_RE = re.compile(
    r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_COMPACT_DATE_RE = re.compile(
    r"(\d{1,2})[.](\d{1,2})[.](\d{4})",
)

_AUTHOR_LINE_RE = re.compile(
    r"^[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]"
    r"+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]+)*\s*$"
)


class YandexBrowserTransportProtocol(Protocol):
    """The slice of the transport the adapter depends on."""

    last_total_count: int | None
    last_average_rating: float | None
    last_product_name: str | None

    def iter_review_batches(
        self,
        product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]: ...


def parse_yandex_date(value: Any) -> datetime | None:
    """Parse a review date in any of the page's shapes.

    Handles the Russian long form (``5 октября 2023``), the ISO form
    (``2023-10-05`` / ``2023-10-05T…``) and ``05.10.2023``. Anything
    else returns ``None`` — an unknown format must not abort the
    stream.
    """
    if not value:
        return None
    text = str(value).strip()

    match = _RU_DATE_RE.search(text)
    if match:
        day = int(match.group(1))
        month = _MONTHS.get(match.group(2).lower())
        year = int(match.group(3))
        if month:
            return _safe_date(year, month, day)

    match = _ISO_DATE_RE.search(text)
    if match:
        return _safe_date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )

    match = _COMPACT_DATE_RE.search(text)
    if match:
        return _safe_date(
            int(match.group(3)),
            int(match.group(2)),
            int(match.group(1)),
        )

    return None


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def normalize_yandex_rating(value: Any) -> int | None:
    """'5' / 5 / 4.7 / None → int in 1..5 (rounded) or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    rounded = round(number)
    if 1 <= rounded <= 5:
        return rounded
    return None


_LABELED_SECTION_RES = (
    (
        "pros",
        re.compile(
            r"Достоинства:\s*(.*?)" r"(?=Недостатки:|Комментарий:|$)",
            re.DOTALL,
        ),
    ),
    (
        "cons",
        re.compile(
            r"Недостатки:\s*(.*?)(?=Комментарий:|$)",
            re.DOTALL,
        ),
    ),
    ("text", re.compile(r"Комментарий:\s*(.*)", re.DOTALL)),
)


def split_labeled_sections(
    blob: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Split a «Достоинства/Недостатки/Комментарий» body blob.

    The reviews page renders the body as labeled sections
    (measured 2026-09-18: ``Достоинства:\\xa0маме помогают.
    Недостатки:\\xa0нет``). When the dedicated ``review-pro`` /
    ``review-contra`` nodes are absent the whole blob arrives as
    the text; this recovers pros/cons/text from the labels.

    Returns ``(pros, cons, text)``; a field is ``None`` when its
    label is missing or empty. Returns ``(None, None, blob)`` when
    no label matches at all (plain review, keep as-is).
    """
    if not blob:
        return None, None, blob

    found: dict[str, str | None] = {}
    for key, regex in _LABELED_SECTION_RES:
        match = regex.search(blob)
        value = match.group(1).strip() if match else ""
        found[key] = value or None

    if not any(found.values()):
        # No labels at all — plain review text.
        return None, None, blob

    # \xa0 and other NBSP-like spaces come glued to words.
    clean = {
        key: (value.replace("\xa0", " ").strip() if value else None)
        for key, value in found.items()
    }
    return clean["pros"], clean["cons"], clean["text"]


def split_author_and_text(blob: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a merged review blob.

    The reviews widget sometimes renders the card as one text blob:
    author line, optional date line, body. A line that looks like a
    capitalized standalone name (no sentence punctuation, ≤3 words)
    is taken for the author; the rest is the body.
    """
    if not blob:
        return None, None
    lines = [line.strip() for line in blob.splitlines() if line.strip()]
    if not lines:
        return None, None

    author: str | None = None
    body_start = 0
    if (
        len(lines) > 1
        and _AUTHOR_LINE_RE.match(lines[0])
        and len(lines[0].split()) <= 3
    ):
        author = lines[0]
        body_start = 1
    body = "\n".join(lines[body_start:]) or None
    return author, body


class YandexMarketAdapter(MarketplaceAdapter):
    """Maps raw Yandex.Market review cards to domain Reviews."""

    name = "yandex"

    def __init__(
        self,
        browser_transport: YandexBrowserTransportProtocol,
    ) -> None:
        self.transport = browser_transport
        #: Filled while iterating (the CLI prints it in the summary).
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        #: Product title for the unified output's ``product_title``.
        self.last_product_title: str | None = None

    async def collect(self, product_url: str) -> ReviewPage:
        """Not implemented: the Yandex flow is streaming-only (the
        reviews list lazy-loads; there is no single-shot page)."""
        raise NotImplementedError(
            "YandexMarketAdapter is streaming-only — "
            "use iter_reviews()"
        )

    async def iter_reviews(
        self,
        product_url: str,
    ) -> AsyncIterator[Review]:
        """Stream deduplicated Reviews for one Yandex.Market product.

        Dedup mirrors the Ozon adapter: primary key = the card's
        ``uuid``; fallback = composite author/date/rating/text key
        (the transport already dedups per run — this is a second
        layer for adapter-level reuse).
        """
        product_id = str(extract_yandex_market_product_id(product_url))
        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=product_id,
        )

        seen: set[str] = set()
        async for batch in self.transport.iter_review_batches(
            product_url,
        ):
            # Refresh the totals on every batch so they survive an
            # early break (the CLI reads them right after its loop).
            self.last_total_count = (
                self.transport.last_total_count
            )
            self.last_average_rating = (
                self.transport.last_average_rating
            )
            self.last_product_title = (
                self.transport.last_product_name
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
        text = self._clean_text(card.get("text"))
        pros = self._clean_text(card.get("pros"))
        cons = self._clean_text(card.get("cons"))
        author = self._clean_text(card.get("author"))

        # The DOM renders the body as labeled sections; when the
        # dedicated nodes are absent, split them out of the blob.
        if text and any(
            label in text
            for label in ("Достоинства:", "Недостатки:", "Комментарий:")
        ):
            label_pros, label_cons, comment = split_labeled_sections(
                text,
            )
            # ``review-description`` ships the WHOLE blob (labels
            # included) even when review-pro/review-contra exist —
            # keep the dedicated nodes, take only the comment part
            # as the text.
            pros = pros or label_pros
            cons = cons or label_cons
            text = comment

        if not author and text:
            author, text = split_author_and_text(text)

        review_id = card.get("uuid")
        review_id = str(review_id) if review_id else None

        photos = [
            url
            for url in (
                card.get("photos") or card.get("images") or []
            )
            if isinstance(url, str) and url
        ]

        return Review(
            review_id=review_id,
            product=product,
            rating=normalize_yandex_rating(card.get("rating")),
            text=text,
            pros=pros,
            cons=cons,
            author=author,
            created_at=parse_yandex_date(card.get("date")),
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
    "YandexMarketAdapter",
    "normalize_yandex_rating",
    "parse_yandex_date",
    "split_author_and_text",
    "split_labeled_sections",
]
