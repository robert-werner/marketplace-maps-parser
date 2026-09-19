# src/shared/unified_format.py
"""The unified review output format shared by every collector.

One JSON document per run::

    {
      "reviews": [
        {
          "source_url": "https://…",
          "platform": "yandex_maps",
          "product_title": "Отделение почтовой связи № 430028",
          "text": "Достоинства: …\\nНедостатки: …\\nКомментарий…",
          "rating": 5,
          "review_date": "2021-12-25",
          "photos": 2,
          "video_len": null,
          "text_len": 157,
          "raw": {…the source card…}
        },
        …
      ],
      "diagnostics": {
        "status": "ok" | "error",
        "error": null | "YandexCaptchaError: …",
        …source-specific extras (total_count, average_rating…)…
      }
    }

Contract rules (2026-09-19):

- ``text`` merges the pros/cons sections into one string when the
  source splits them (Ozon, Yandex.Market); ``text_len`` is
  ``len(text.strip())`` of the FINAL merged text.
- ``photos`` is a COUNT (the domain ``Review.photos`` list of urls
  collapses to its length); 0 when absent.
- ``video_len`` is seconds when the source card carries a duration
  (``videos``/``media`` items with a duration-ish field), ``null``
  when there is no video or no known duration.
- A failed run (captcha, block, transport error) yields
  ``reviews: []`` — or the partial batch collected before the
  failure — with the reason in ``diagnostics``; a captcha page is
  NEVER emitted as a review.
- ``review_id`` is not a schema field: it lives inside ``raw``
  (``reviewId`` / ``id`` / ``uuid`` / …) and is recovered from
  there for ``--resume`` dedup.
"""
from __future__ import annotations

from typing import Any

from domain.entities import Review

_TEXT_PART_LABELS = (
    ("pros", "Достоинства"),
    ("cons", "Недостатки"),
)

#: Fields of a review record, in the canonical order.
UNIFIED_REVIEW_FIELDS = (
    "source_url",
    "platform",
    "product_title",
    "text",
    "rating",
    "review_date",
    "photos",
    "video_len",
    "text_len",
    "raw",
)

_VIDEO_CONTAINERS = ("videos", "video", "media")
_VIDEO_DURATION_KEYS = (
    "duration",
    "durationSeconds",
    "duration_seconds",
    "seconds",
    "length",
)

_RAW_ID_KEYS = ("reviewId", "review_id", "uuid", "id")


def compose_review_text(review: Review) -> str | None:
    """One text string: labeled pros/cons sections + the body."""
    parts: list[str] = []
    for attr, label in _TEXT_PART_LABELS:
        value = getattr(review, attr, None)
        if value and str(value).strip():
            parts.append(f"{label}: {str(value).strip()}")
    if review.text and review.text.strip():
        parts.append(review.text.strip())
    return "\n".join(parts) or None


def video_len_from_raw(raw: dict[str, Any]) -> float | None:
    """Video duration (seconds) best-effort from the source card."""
    if not isinstance(raw, dict):
        return None
    for key in _VIDEO_CONTAINERS:
        items = raw.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for duration_key in _VIDEO_DURATION_KEYS:
                value = item.get(duration_key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value > 0
                ):
                    return float(value)
    return None


def normalize_unified_rating(
    value: Any,
) -> int | None:
    """Stars 1..5 as an int; anything else is ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    rounded = round(number)
    if 1 <= rounded <= 5:
        return int(rounded)
    return None


def build_unified_review(
    review: Review,
    *,
    product_title: str | None = None,
) -> dict[str, Any]:
    text = compose_review_text(review)
    created_at = review.created_at
    return {
        "source_url": review.product.source_url,
        "platform": review.product.marketplace,
        "product_title": product_title,
        "text": text,
        "rating": normalize_unified_rating(review.rating),
        "review_date": (
            created_at.strftime("%Y-%m-%d")
            if created_at
            else None
        ),
        "photos": len(review.photos or []),
        "video_len": video_len_from_raw(review.raw or {}),
        "text_len": len(text.strip()) if text else 0,
        "raw": review.raw,
    }


def build_unified_document(
    reviews: list[dict[str, Any]],
    *,
    error: str | None = None,
    **diagnostics: Any,
) -> dict[str, Any]:
    """The run document: ``reviews`` + ``diagnostics``.

    ``status`` is ``"error"`` when an error reason is given (the
    reviews list may still hold the partial batch collected before
    the failure)."""
    document: dict[str, Any] = {
        "reviews": reviews,
        "diagnostics": {
            "status": "error" if error else "ok",
            "error": error,
            **diagnostics,
        },
    }
    return document


def unified_review_id(record: dict[str, Any]) -> str | None:
    """Recover the source review id from a unified record's ``raw``
    (for ``--resume`` dedup)."""
    raw = record.get("raw")
    if not isinstance(raw, dict):
        return None
    for key in _RAW_ID_KEYS:
        value = raw.get(key)
        if value:
            return str(value)
    return None


__all__ = [
    "UNIFIED_REVIEW_FIELDS",
    "build_unified_document",
    "build_unified_review",
    "compose_review_text",
    "normalize_unified_rating",
    "unified_review_id",
    "video_len_from_raw",
]
