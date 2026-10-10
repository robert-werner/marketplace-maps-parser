"""Wildberries live DOM review adapter."""
from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, Protocol

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from shared.async_iterators import closing_iterator
from shared.url_parsers import extract_nm_id

_MONTHS = {
    name: index for index, name in enumerate((
        'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
        'июля', 'августа', 'сентября', 'октября', 'ноября',
        'декабря',
    ), start=1)
}
_DATE_RE = re.compile(
    r'(?:(Сегодня|Вчера)|(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?)',
    re.I,
)


class WildberriesDomTransport(Protocol):
    last_total_count: int | None
    last_average_rating: float | None
    last_product_title: str | None

    def iter_review_batches(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]: ...


class WildberriesAdapter(MarketplaceAdapter):
    name = 'wildberries'

    def __init__(self, transport: WildberriesDomTransport) -> None:
        self.transport = transport
        self.last_product_title: str | None = None
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None

    async def collect(self, product_url: str) -> ReviewPage:
        reviews = [
            review async for review in self.iter_reviews(product_url)
        ]
        return ReviewPage(
            product=ProductRef(
                self.name,
                product_url,
                str(extract_nm_id(product_url)),
            ),
            reviews=reviews,
            total_count=self.last_total_count,
            average_rating=self.last_average_rating,
        )

    async def iter_reviews(self, product_url: str) -> AsyncIterator[Review]:
        product = ProductRef(
            self.name, product_url, str(extract_nm_id(product_url)),
        )
        seen: set[str] = set()
        async with closing_iterator(
            self.transport.iter_review_batches(product_url)
        ) as owned_stream:
            async for batch in owned_stream:
                self.last_product_title = self.transport.last_product_title
                self.last_total_count = self.transport.last_total_count
                self.last_average_rating = self.transport.last_average_rating
                for item in batch:
                    review = self._map_review(item, product)
                    if review.review_id not in seen:
                        seen.add(review.review_id or '')
                        yield review
        self.last_product_title = self.transport.last_product_title
        self.last_total_count = self.transport.last_total_count
        self.last_average_rating = self.transport.last_average_rating

    @staticmethod
    def _map_review(item: dict[str, Any], product: ProductRef) -> Review:
        sections = {
            str(section.get('label') or '').rstrip(':').lower():
            section.get('value')
            for section in item.get('sections') or []
            if isinstance(section, dict)
        }
        text = sections.get('комментарий') or item.get('text')
        pros = sections.get('достоинства') or item.get('pros')
        cons = sections.get('недостатки') or item.get('cons')
        raw_date = str(item.get('date') or '')
        created_at = WildberriesAdapter._parse_date(raw_date)
        date_key = raw_date
        if created_at is not None:
            date_key = re.sub(
                r'^(Сегодня|Вчера)',
                created_at.strftime('%Y-%m-%d'),
                date_key,
                flags=re.I,
            )
        # No review id is exposed in the DOM. Keep a reproducible ID
        # across runs even when relative dates turn from today to yesterday.
        identity = '\x1f'.join(str(v or '') for v in (
            product.product_id, item.get('author'), date_key,
            item.get('rating'), text, pros, cons,
        ))
        review_id = str(item.get('id') or hashlib.sha256(
            identity.encode('utf-8'),
        ).hexdigest())
        raw = {**item, 'id': review_id, 'dom': not item.get('api', False)}
        return Review(
            review_id=review_id,
            product=product,
            rating=item.get('rating'),
            text=text,
            pros=pros,
            cons=cons,
            author=item.get('author'),
            created_at=created_at,
            seller_answer=item.get('answer'),
            photos=list(item.get('photos') or []),
            raw=raw,
        )

    @staticmethod
    def _parse_date(value: str) -> datetime | None:
        if re.match(r"^\d{4}-\d{2}-\d{2}", value):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        match = _DATE_RE.search(value)
        if not match:
            return None
        now = datetime.now()
        if match.group(1):
            return now.replace(
                hour=0, minute=0, second=0, microsecond=0,
            ) - timedelta(days=match.group(1).lower() == 'вчера')
        month = _MONTHS.get(match.group(3).lower())
        if month is None:
            return None
        year = int(match.group(4)) if match.group(4) else now.year
        try:
            result = datetime(year, month, int(match.group(2)))
            if not match.group(4) and result > now:
                result = result.replace(year=year - 1)
            return result
        except ValueError:
            return None
