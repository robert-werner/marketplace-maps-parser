from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from shared.url_parsers import (
    extract_ozon_product_id,
    extract_ozon_product_path,
)


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{12}$",
    re.IGNORECASE,
)


class OzonAdapter(MarketplaceAdapter):
    name = "ozon"

    def __init__(self, browser_transport) -> None:
        self.browser_transport = browser_transport

    async def collect(self, product_url: str) -> ReviewPage:
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        raw = await self.browser_transport.get_ozon_reviews_json(
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

        seen_keys: set[str] = set()

        async for page_number, payload in (
                self.browser_transport.iter_ozon_reviews_json(
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

            for position, review in enumerate(reviews):
                key = review.review_id

                if not key:
                    key = build_review_key(
                        review,
                        page_number=page_number,
                        position=position,
                    )

                if key in seen_keys:
                    continue

                seen_keys.add(key)
                new_count += 1
                yield review

            print(
                f"Ozon: страница {page_number}; "
                f"получено={len(reviews)}; "
                f"новых={new_count}"
            )

    def parse_ozon_dom_card(self,
            card: dict[str, Any],
            product: ProductRef,
    ) -> Review:
        text = card.get("text") or ""

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        author = lines[1] if len(lines) > 1 else (
            lines[0] if lines else None
        )

        return Review(
            review_id=card.get("uuid"),
            product=product,
            rating=None,
            text=text or None,
            author=author,
            created_at=parse_ozon_date(
                card.get("published_at")
            ),
            raw=card,
        )

    async def iter_reviews_by_scroll(
            self,
            product_url: str,
            *,
            max_reviews: int | None = None,
    ) -> AsyncIterator[Review]:
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        async for cards in (
                self.browser_transport.iter_ozon_reviews_by_scroll(
                    product_path=product_path,
                    max_reviews=max_reviews,
                )
        ):
            for position, card in enumerate(cards):
                review = self.parse_ozon_dom_card(
                    card=card,
                    product=product,
                )

                if review is not None:
                    yield review

    async def collect_all(
        self,
        product_url: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> ReviewPage:
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

        key = review.review_id or build_review_key(
            review,
            page_number=0,
            position=len(result),
        )

        if key in seen_ids:
            continue

        seen_ids.add(key)
        result.append(review)

    return result


def walk_json(
    value: Any,
    *,
    key_name: str | None = None,
) -> Iterator[Any]:
    if isinstance(value, dict):
        current = dict(value)

        if key_name:
            current["_ozon_key"] = key_name

        yield current

        for key, child in value.items():
            yield from walk_json(
                child,
                key_name=str(key),
            )

        return

    if isinstance(value, list):
        yield value

        for child in value:
            yield from walk_json(child)

        return

    yield value

    if not isinstance(value, str):
        return

    text = value.strip()

    if not text or text[0] not in "[{":
        return

    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return

    yield from walk_json(
        decoded,
        key_name=key_name,
    )


def map_ozon_review_node(
    node: dict[str, Any],
    product: ProductRef,
) -> Review | None:
    review_id = extract_review_id(node)

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
        review_id=review_id,
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


def extract_review_id(
    node: dict[str, Any],
) -> str | None:
    value = first_value(
        node,
        "reviewId",
        "review_id",
        "reviewID",
        "reviewUuid",
        "review_uuid",
        "feedbackId",
        "feedback_id",
        "commentId",
        "comment_id",
        "uuid",
    )

    if value is not None:
        return str(value)

    key_name = node.get("_ozon_key")

    if isinstance(key_name, str) and UUID_RE.fullmatch(key_name):
        return key_name

    return None


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
    keys = {
        str(key).lower()
        for key in node
    }

    has_id = review_id is not None

    has_review_marker = bool(
        keys
        & {
            "reviewid",
            "review_id",
            "reviewuuid",
            "review_uuid",
            "uuid",
            "publishedat",
            "published_at",
            "createdat",
            "created_at",
            "author",
            "authorname",
            "username",
            "statusid",
            "rating",
            "score",
            "stars",
            "reviewtext",
            "review_text",
            "comment",
            "advantages",
            "disadvantages",
        }
    )

    return has_id and has_review_marker


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


def build_review_key(
    review: Review,
    *,
    page_number: int,
    position: int,
) -> str:
    return "|".join(
        (
            review.product.product_id,
            str(page_number),
            str(position),
            review.author or "",
            str(review.created_at),
            str(review.rating),
            review.text or "",
        )
    )
