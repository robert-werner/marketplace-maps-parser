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
- ``video_count`` is the number of video items in the raw card
  (0 when absent or empty); works with both string URLs and
  object payloads.
- ``video_durations`` is a list of numeric durations (seconds)
  collected from every video item that exposes a duration key;
  empty list ``[]`` when there are no video objects with durations.
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
    "video_count",
    "video_durations",
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


def _parse_duration_value(value: Any) -> float | None:
    """Parse a duration value to seconds.

    Handles numeric seconds, ISO 8601 ``PT1M24S``, and ``MM:SS`` /
    ``HH:MM:SS`` strings.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None

    text = str(value).strip()
    if not text:
        return None

    # ISO 8601 duration (e.g. PT1M24S) — grab the numeric part
    if text.startswith("P"):
        import re as _re
        m = _re.search(r"(?:^|[\s])PT\s*([\d.]+)[\s]*[SM]", text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

    # MM:SS or HH:MM:SS (Ozon style: ``"00:24"``, ``"01:31"``)
    parts = text.split(":")
    try:
        if len(parts) == 2:  # MM:SS
            return float(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:  # HH:MM:SS
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass

    return None


def _find_video_items(raw: Any) -> list[Any]:
    """Collect video items from a raw review dict.

    Works with top-level ``videos``/``video``/``media`` (Yandex, WB)
    and nested ``content.videos`` (Ozon).

    Also handles the Yandex.Market fast path where videos is a list
    of numeric durations (seconds) or video IDs (strings).
    """
    items: list[Any] = []
    if not isinstance(raw, dict):
        return items
    # Top-level containers
    for key in _VIDEO_CONTAINERS:
        val = raw.get(key)
        if isinstance(val, list):
            items.extend(val)
    # Nested content (Ozon)
    content = raw.get("content")
    if isinstance(content, dict):
        for key in _VIDEO_CONTAINERS:
            val = content.get(key)
            if isinstance(val, list):
                items.extend(val)
    return items




def video_len_from_raw(raw: dict[str, Any]) -> float | None:
    """Video duration (seconds) best-effort from the source card.

    Handles both video-object lists and numeric-duration lists
    (Yandex.Market fast path).
    """
    items = _find_video_items(raw)
    for item in items:
        # Direct numeric value (fast path)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if item > 0:
                return float(item)
        # Video object with duration key
        elif isinstance(item, dict):
            for duration_key in _VIDEO_DURATION_KEYS:
                parsed = _parse_duration_value(item.get(duration_key))
                if parsed is not None and parsed > 0:
                    return parsed
    return None


def video_count_from_raw(raw: Any) -> int:
    """Count of video items in the source card.

    Works with both string URLs (as emitted by the WB browser
    transport) and object payloads (as emitted by Ozon / Yandex).
    """
    items = _find_video_items(raw)
    return len(items)


def video_durations_from_raw(raw: Any) -> list[float]:
    """Collect every duration (seconds) from video objects in the
    source card.

    Handles both video-object lists and numeric-duration lists
    (Yandex.Market fast path).
    """
    items = _find_video_items(raw)
    durations: list[float] = []
    for item in items:
        # Direct numeric value (fast path)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if item > 0:
                durations.append(float(item))
                continue
        # Video object with duration key
        if isinstance(item, dict):
            for duration_key in _VIDEO_DURATION_KEYS:
                value = item.get(duration_key)
                parsed = _parse_duration_value(value)
                if parsed is not None and parsed > 0:
                    durations.append(parsed)
                    break  # one duration per video item
    return durations


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
    raw = dict(review.raw)
    if review.review_id and not any(raw.get(key) for key in _RAW_ID_KEYS):
        raw["review_id"] = review.review_id
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
        "video_count": video_count_from_raw(review.raw or {}),
        "video_durations": video_durations_from_raw(review.raw or {}),
        "video_len": video_len_from_raw(review.raw or {}),
        "text_len": len(text.strip()) if text else 0,
        "raw": raw,
    }


def build_unified_document(
    reviews: list[dict[str, Any]],
    *,
    error: str | None = None,
    status: str | None = None,
    **diagnostics: Any,
) -> dict[str, Any]:
    """The run document: ``reviews`` + ``diagnostics``.

    ``status`` is one of ``complete``, ``partial`` or ``failed``.
    The caller may provide a stronger completeness decision when it
    knows the source-side total; otherwise errors are classified from
    whether any records survived."""
    if status is None:
        if error:
            status = "partial" if reviews else "failed"
        else:
            status = "complete"
    document: dict[str, Any] = {
        "reviews": reviews,
        "diagnostics": {
            "status": status,
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
    "video_count_from_raw",
    "video_durations_from_raw",
    "video_len_from_raw",
]
