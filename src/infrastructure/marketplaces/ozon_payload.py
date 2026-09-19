# src/infrastructure/marketplaces/ozon_payload.py
"""Ozon payload parsing: the fuzzy walker and the node mappers.

Extracted from ``marketplaces/ozon.py`` (which keeps the transport
orchestration and re-exports these names for backward compatibility).
Everything here is a pure function over the raw Ozon pagination
payload — no transport dependencies — so both the adapter and the
transports can import this module freely.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from domain.entities import ProductRef, Review

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{4}-"
    r"[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ``"34687 отзыв на Лосьон для роста волос… от покупателей"`` —
# the seo title of a pdp_reviews page; the product name is the
# middle part.
_SEO_REVIEW_TITLE_RE = re.compile(
    r"\d[\d\s\u00a0]*\s*отзыв\w*\s+на\s+(?P<name>.+?)"
    r"(?:\s+от\s+покупателей.*)?$",
    re.DOTALL,
)


def extract_ozon_product_title(
    payload: dict[str, Any],
) -> str | None:
    """Product title from a pdp_reviews payload.

    Primary source: ``payload["seo"]["title"]`` (``"N отзыв на
    <name> от покупателей"``). Fallback: scan every string in the
    payload for the same pattern — the DOM-page payloads built by
    the public_page transport repeat it in their meta tags.
    """
    seo = payload.get("seo")
    title = seo.get("title") if isinstance(seo, dict) else None
    if isinstance(title, str):
        match = _SEO_REVIEW_TITLE_RE.search(title)
        if match:
            name = match.group("name").strip()
            if name:
                return name
    return _scan_seo_title(payload)


def _scan_seo_title(node: Any) -> str | None:
    if isinstance(node, dict):
        for value in node.values():
            found = _scan_seo_title(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _scan_seo_title(value)
            if found:
                return found
    elif isinstance(node, str):
        match = _SEO_REVIEW_TITLE_RE.search(node)
        if match:
            name = match.group("name").strip()
            # Plausible product names only, not UI phrases that
            # happen to match ("12 отзывов на товар" etc.).
            if 5 <= len(name) <= 300:
                return name
    return None


def extract_ozon_rating_summary(
    payload: dict[str, Any],
    *,
    product_id: str | None = None,
    product_url: str | None = None,
) -> dict[str, Any] | None:
    """Extract the rating histogram from a pdp_reviews payload.

    Ozon's ``webReviewProductScore`` widget state carries the
    per-star counts (e.g. ``{"5 звёзд": 4093, ...}``), the total
    ratings count and the average score. Rating-only «оценки» are
    NOT exposed as individual review cards anywhere — the histogram
    is the only place they exist — so this summary is what allows
    the CLI to account for them (summary file + optional synthetic
    rows via ``--include-rating-only``).

    Returns ``None`` when the payload has no score widget (e.g.
    DOM-card payloads from the public_page transport).
    """
    widget_states = payload.get("widgetStates")
    if not isinstance(widget_states, dict):
        return None

    for name, raw in widget_states.items():
        if "webReviewProductScore" not in str(name):
            continue

        try:
            widget = (
                json.loads(raw) if isinstance(raw, str) else raw
            )
        except (TypeError, ValueError):
            continue

        if not isinstance(widget, dict):
            continue

        score_rows = widget.get("score")
        if not isinstance(score_rows, list):
            continue

        histogram: dict[str, int] = {}
        for row in score_rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title", "")).strip()
            value = row.get("value")
            digit = re.search(r"\d", title)
            if not title or digit is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            histogram[digit.group(0)] = value

        if not histogram:
            return None

        return {
            "product_id": product_id,
            "product_url": product_url,
            "histogram": histogram,
            "reviews_count": widget.get("reviewsCount"),
            "average_score": widget.get("totalScore"),
        }

    return None


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
    if isinstance(author, str):
        return author or None
    return None


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
            tz=UTC,
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
            ).replace(tzinfo=UTC)
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
