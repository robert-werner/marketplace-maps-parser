from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from shared.url_parsers import (
    extract_ozon_product_id,
    extract_ozon_product_path,
)


class OzonAdapter(MarketplaceAdapter):
    name = "ozon"

    def __init__(self, browser_transport) -> None:
        self.browser_transport = browser_transport

    async def collect(self, product_url: str) -> ReviewPage:
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        raw = await self.browser_transport.get_ozon_reviews_json(
            product_url=product_url,
            product_path=product_path,
            page_number=1,
        )

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        reviews = extract_reviews_from_ozon_payload(
            payload=raw,
            product=product,
        )

        return ReviewPage(
            product=product,
            reviews=reviews,
            total_count=len(reviews),
            raw=raw,
        )

    async def iter_reviews(
            self,
            product_url: str,
            *,
            start_page: int = 1,
            max_pages: int | None = None,
    ) -> AsyncIterator[Review]:
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        seen_ids: set[str] = set()

        async for page_number, payload in (
                self.browser_transport.iter_ozon_reviews_json(
                    product_url=product_url,
                    product_path=product_path,
                    start_page=start_page,
                    max_pages=max_pages,
                )
        ):
            reviews = extract_reviews_from_ozon_payload(
                payload=payload,
                product=product,
            )

            new_count = 0

            for review in reviews:
                key = (
                        review.review_id
                        or build_review_key(review)
                )

                if key in seen_ids:
                    continue

                seen_ids.add(key)
                new_count += 1
                yield review

            print(
                f"Ozon: страница {page_number}, "
                f"найдено {len(reviews)}, "
                f"новых {new_count}"
            )
    async def collect_all(
        self,
        product_url: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> ReviewPage:
        """Собирает все найденные страницы в один ReviewPage."""
        product_id = extract_ozon_product_id(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        reviews: list[Review] = []

        async for review in self.iter_reviews(
            product_url=product_url,
            start_page=start_page,
            max_pages=max_pages,
        ):
            reviews.append(review)

        return ReviewPage(
            product=product,
            reviews=reviews,
            total_count=len(reviews),
        )


def extract_reviews_from_ozon_payload(
    payload: dict[str, Any],
    product: ProductRef,
) -> list[Review]:
    result: list[Review] = []
    seen_ids: set[str] = set()

    for node in walk_json(payload):
        if not isinstance(node, dict):
            continue

        review = map_ozon_review_node(
            node=node,
            product=product,
        )

        if review is None:
            continue

        key = review.review_id or build_review_key(review)

        if key in seen_ids:
            continue

        seen_ids.add(key)
        result.append(review)

    return result


def walk_json(value: Any) -> Iterator[Any]:
    yield value

    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
        return

    if isinstance(value, list):
        for child in value:
            yield from walk_json(child)
        return

    if not isinstance(value, str):
        return

    text = value.strip()

    if not text or text[0] not in "[{":
        return

    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return

    yield from walk_json(decoded)


def map_ozon_review_node(
    node: dict[str, Any],
    product: ProductRef,
) -> Review | None:
    review_id = first_value(
        node,
        "reviewId",
        "review_id",
        "reviewID",
        "reviewUuid",
        "review_uuid",
        "uuid",
    )

    text = first_value(
        node,
        "text",
        "reviewText",
        "review_text",
        "content",
        "comment",
        "description",
    )

    rating = first_value(
        node,
        "rating",
        "score",
        "stars",
        "productRating",
        "product_rating",
        "valuation",
    )

    if not is_review_node(
        node=node,
        review_id=review_id,
        text=text,
        rating=rating,
    ):
        return None

    return Review(
        review_id=(
            str(review_id)
            if review_id is not None
            else None
        ),
        product=product,
        rating=normalize_rating(rating),
        text=normalize_text(text),
        pros=normalize_text(
            first_value(
                node,
                "pros",
                "advantages",
                "pluses",
            )
        ),
        cons=normalize_text(
            first_value(
                node,
                "cons",
                "disadvantages",
                "minuses",
            )
        ),
        author=normalize_text(
            first_value(
                node,
                "author",
                "authorName",
                "author_name",
                "userName",
                "user_name",
                "reviewerName",
            )
        ),
        created_at=parse_ozon_date(
            first_value(
                node,
                "createdAt",
                "created_at",
                "publishedAt",
                "published_at",
                "date",
                "createdDate",
            )
        ),
        seller_answer=extract_seller_answer(node),
        raw=node,
    )


def first_value(
    node: dict[str, Any],
    *keys: str,
) -> Any:
    for key in keys:
        value = node.get(key)

        if value is not None and value != "":
            return value

    return None


def is_review_node(
    *,
    node: dict[str, Any],
    review_id: Any,
    text: Any,
    rating: Any,
) -> bool:
    keys = {str(key).lower() for key in node}

    review_markers = {
        "reviewid",
        "review_id",
        "reviewuuid",
        "review_uuid",
        "reviewtext",
        "review_text",
        "publishedat",
        "published_at",
        "advantages",
        "disadvantages",
    }

    has_marker = bool(keys & review_markers)
    has_content = text is not None or rating is not None

    return (
        review_id is not None and has_content
    ) or (
        has_marker and has_content
    )


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        value = value.strip()
        return value or None

    return str(value)


def normalize_rating(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)

    text = str(value).strip().replace(",", ".")

    try:
        number = float(text)
    except ValueError:
        return None

    return int(number) if number.is_integer() else number


def parse_ozon_date(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)

        if timestamp > 10_000_000_000:
            timestamp /= 1000

        return datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        )

    text = str(value).strip()

    if not text:
        return None

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00"),
        )
    except ValueError:
        pass

    for pattern in (
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(
                text,
                pattern,
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def extract_seller_answer(
    node: dict[str, Any],
) -> str | None:
    answer = first_value(
        node,
        "sellerAnswer",
        "seller_answer",
        "answer",
        "merchantAnswer",
        "merchant_answer",
    )

    if isinstance(answer, dict):
        answer = first_value(
            answer,
            "text",
            "content",
            "message",
        )

    return normalize_text(answer)


def build_review_key(review: Review) -> str:
    return "|".join(
        (
            review.product.product_id,
            review.author or "",
            review.text or "",
            str(review.rating),
            str(review.created_at),
        )
    )
