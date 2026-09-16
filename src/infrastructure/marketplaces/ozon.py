from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any, Protocol

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from shared.url_parsers import (
    extract_ozon_product_id,
    extract_ozon_product_path,
)


class OzonBrowserTransport(Protocol):
    """Subset of BrowserJsonTransport / BrowserDomTransport used by OzonAdapter."""

    def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = ...,
        max_pages: int | None = ...,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]: ...

    async def get_ozon_reviews_json(
        self,
        product_path: str,
        *,
        page_number: int = ...,
    ) -> dict[str, Any]: ...

    def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = ...,
    ) -> AsyncIterator[list[dict[str, Any]]]: ...

    def iter_all_ozon_reviews(
        self,
        product_path: str,
        *,
        max_reviews: int | None = ...,
        pagination_max_pages: int | None = ...,
        pagination_start_page: int = ...,
        scroll_max_rounds: int = ...,
        page_delay_seconds: float = ...,
        scroll_pause_seconds: float = ...,
        retry_attempts: int = ...,
    ) -> AsyncIterator[
        tuple[str, dict[str, Any]]
    ]: ...


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

    def __init__(
        self,
        browser_transport: OzonBrowserTransport,
    ) -> None:
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
        # ``text`` from the DOM is multi-line: avatar initials, author
        # name, date, review text, "Вам помог этот отзыв?", "Да N Нет M".
        # We split into lines to extract author and review text.
        raw_text = card.get("text") or ""
        # ``review_text`` is a pre-extracted cleaner version when the
        # DOM transport already did the line-by-line filtering.
        text = card.get("review_text") or raw_text

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        # Author extraction: DOM transport already extracts this when
        # available; fall back to line-based heuristic for raw cards.
        author = card.get("author")
        if author is None:
            author = lines[1] if len(lines) > 1 else (
                lines[0] if lines else None
            )

        # Rating: DOM transport extracts it from SVG star colors
        # (BrowserDomTransport._read_review_rating). When the scroll
        # mode in browser_json.py is used, rating is not extracted
        # (see _read_review_cards in browser_json.py); we fall back to
        # the explicit "rating" key in the card dict if present.
        rating = card.get("rating")

        return Review(
            review_id=card.get("uuid"),
            product=product,
            rating=rating,
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

    # ------------------------------------------------------------------
    # Unified "collect ALL reviews" stream
    # ------------------------------------------------------------------
    async def iter_all_reviews(
            self,
            product_url: str,
            *,
            strategy: str = "auto",
            max_reviews: int | None = None,
            pagination_max_pages: int | None = None,
            pagination_start_page: int = 1,
            scroll_max_rounds: int = 500,
            page_delay_seconds: float = 1.5,
            scroll_pause_seconds: float = 1.0,
            retry_attempts: int = 3,
    ) -> AsyncIterator[Review]:
        """Stream every review for an Ozon product.

        Strategy modes:

        - ``"auto"`` (default): run pagination first, then scroll as a
          fallback / supplement. Reviews are deduplicated by their
          stable id (``reviewId`` / ``uuid``) across both strategies.
        - ``"pagination"``: only the internal Ozon API pagination.
        - ``"scroll"``: only the DOM scroll.

        Always streams — never materializes the full set in memory.
        """
        from shared.logging import get_logger

        log = get_logger("marketplaces.ozon")

        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        seen_keys: set[str] = set()
        yielded = 0

        if strategy == "pagination":
            async for review in self.iter_reviews(
                product_url=product_url,
                start_page=pagination_start_page,
                max_pages=pagination_max_pages,
            ):
                yield review
                yielded += 1
                if (
                    max_reviews is not None
                    and yielded >= max_reviews
                ):
                    return
            return

        if strategy == "scroll":
            async for review in self.iter_reviews_by_scroll(
                product_url=product_url,
                max_reviews=max_reviews,
            ):
                yield review
            return

        if strategy != "auto":
            raise ValueError(
                f"Unknown strategy: {strategy!r}. "
                "Use 'auto', 'pagination', or 'scroll'."
            )

        # ------------------ auto: pagination + scroll ------------------
        if not hasattr(
            self.browser_transport, "iter_all_ozon_reviews"
        ):
            log.warning(
                "Ozon: transport does not implement "
                "iter_all_ozon_reviews; using adapter-level fallback."
            )
            async for review in self._iter_all_reviews_adapter_fallback(
                product=product,
                product_path=product_path,
                max_reviews=max_reviews,
                pagination_max_pages=pagination_max_pages,
                pagination_start_page=pagination_start_page,
                retry_attempts=retry_attempts,
            ):
                yield review
            return

        async for strategy_name, node in (
            self.browser_transport.iter_all_ozon_reviews(
                product_path=product_path,
                max_reviews=max_reviews,
                pagination_max_pages=pagination_max_pages,
                pagination_start_page=pagination_start_page,
                scroll_max_rounds=scroll_max_rounds,
                page_delay_seconds=page_delay_seconds,
                scroll_pause_seconds=scroll_pause_seconds,
                retry_attempts=retry_attempts,
            )
        ):
            if strategy_name == "pagination":
                review = map_ozon_review_node(
                    node=node,
                    product=product,
                )
            else:
                review = self.parse_ozon_dom_card(
                    card=node,
                    product=product,
                )

            if review is None:
                continue

            key = review.review_id or build_review_key(
                review,
                page_number=0,
                position=yielded,
            )

            if key in seen_keys:
                continue
            seen_keys.add(key)

            yield review
            yielded += 1

            if (
                max_reviews is not None
                and yielded >= max_reviews
            ):
                log.info(
                    "Ozon: reached max_reviews={}, stopping",
                    max_reviews,
                )
                return

        log.info(
            "Ozon: auto strategy done, {} unique reviews",
            yielded,
        )

    async def _iter_all_reviews_adapter_fallback(
            self,
            product: ProductRef,
            product_path: str,
            *,
            max_reviews: int | None,
            pagination_max_pages: int | None,
            pagination_start_page: int,
            retry_attempts: int = 3,
    ) -> AsyncIterator[Review]:
        """Used when the transport doesn't implement ``iter_all_ozon_reviews``.

        Runs pagination first, then scroll, with adapter-level
        cross-strategy dedup by ``review_id``.
        """
        from shared.logging import get_logger

        log = get_logger("marketplaces.ozon")
        seen_keys: set[str] = set()
        yielded = 0

        # --- pagination ---
        try:
            async for page_num, payload in (
                self.browser_transport.iter_ozon_reviews_json(
                    product_path=product_path,
                    start_page=pagination_start_page,
                    max_pages=pagination_max_pages,
                    retry_attempts=retry_attempts,
                )
            ):
                reviews = extract_reviews_from_ozon_payload(
                    payload=payload,
                    product=product,
                )
                for review in reviews:
                    key = review.review_id or build_review_key(
                        review, page_number=page_num, position=yielded,
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    yield review
                    yielded += 1
                    if (
                        max_reviews is not None
                        and yielded >= max_reviews
                    ):
                        return
        except Exception as exc:
            log.warning(
                "Ozon: pagination failed in fallback: {} — "
                "trying scroll only",
                exc,
            )

        # --- scroll ---
        try:
            async for cards in (
                self.browser_transport.iter_ozon_reviews_by_scroll(
                    product_path=product_path,
                    max_reviews=None,
                )
            ):
                for card in cards:
                    review = self.parse_ozon_dom_card(
                        card=card, product=product,
                    )
                    if review is None:
                        continue
                    key = review.review_id or build_review_key(
                        review, page_number=0, position=yielded,
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    yield review
                    yielded += 1
                    if (
                        max_reviews is not None
                        and yielded >= max_reviews
                    ):
                        return
        except Exception as exc:
            log.error(
                "Ozon: scroll failed in fallback: {}", exc,
            )
            if yielded == 0:
                raise

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


def _ozon_author_name(author: Any) -> str | None:
    """The pdp_reviews API sends the author as an object; flatten
    firstName/lastName into a display name."""
    if isinstance(author, dict):
        return (
            " ".join(
                filter(
                    None,
                    (author.get("firstName"), author.get("lastName")),
                )
            )
            or None
        )
    return author


def map_ozon_review_node(
    node: dict[str, Any],
    product: ProductRef,
) -> Review | None:
    review_id = extract_review_id(node)

    # The pdp_reviews API nests the payload: node["content"] =
    # {comment, score, positive, negative, photos, videos}. The
    # legacy flat shapes stay supported; note "content" must NOT be
    # tried as a text field — it is a dict and first_value would
    # return it whole (measured: whole-review str() in Review.text).
    content = node.get("content")
    if not isinstance(content, dict):
        content = {}

    text = first_value(
        node,
        "text",
        "reviewText",
        "review_text",
        "comment",
        "description",
    )
    if not text:
        text = content.get("comment")

    rating = first_value(
        node,
        "rating",
        "score",
        "stars",
        "productRating",
        "product_rating",
        "valuation",
    )
    if rating is None:
        rating = content.get("score")

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
            or content.get("positive")
        ),
        cons=normalize_text(
            first_value(
                node,
                "cons",
                "disadvantages",
                "minuses",
            )
            or content.get("negative")
        ),
        author=normalize_text(
            _ozon_author_name(
                first_value(
                    node,
                    "author",
                    "authorName",
                    "author_name",
                    "userName",
                    "user_name",
                    "reviewerName",
                )
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
    """Heuristic for deciding whether a dict in the Ozon payload is a
    review node.

    A node is a review if it has a stable ``review_id`` (or UUID key)
    AND at least one "review-ish" marker key (rating, author, date,
    pros/cons, text, etc.).

    Reviews with rating-only (no text) are accepted — many Ozon
    shoppers leave a star rating without writing anything, and we
    want to collect those too. The ``has_review_marker`` check covers
    them because they still have ``rating`` / ``score`` / ``stars``
    keys in the JSON.
    """
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
