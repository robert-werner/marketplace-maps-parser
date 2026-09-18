# src/infrastructure/marketplaces/ozon_dom_parser.py
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from domain.entities import ProductRef, Review

HELPFUL_VOTES_RE = re.compile(
    r"Да\s+(\d+)\s+Нет\s+(\d+)"
)

DATE_RE = re.compile(
    r"^\d{1,2}\s+"
    r"(?:января|февраля|марта|апреля|мая|июня|"
    r"июля|августа|сентября|октября|ноября|декабря)"
    r"\s+\d{4}$",
    re.IGNORECASE,
)


def parse_ozon_review_card(
    card: dict[str, Any],
    product: ProductRef,
) -> Review:
    if not isinstance(card, dict):
        raise TypeError(
            "Ожидался словарь отзыва, "
            f"получен {type(card).__name__}"
        )

    text = card.get("text") or ""

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    author = _extract_author(lines)
    review_date = _extract_date(lines)
    review_text = _extract_review_text(
        lines=lines,
        author=author,
        review_date=review_date,
    )

    published_at = card.get("published_at")
    created_at = _parse_timestamp(published_at)

    votes_match = HELPFUL_VOTES_RE.search(text)

    helpful_yes = (
        int(votes_match.group(1))
        if votes_match
        else None
    )

    helpful_no = (
        int(votes_match.group(2))
        if votes_match
        else None
    )

    rating = card.get("rating")

    if rating is None:
        filled = card.get("stars_filled")
        total = card.get("stars_total")

        if filled is not None and total:
            rating = int(filled)

    return Review(
        review_id=card.get("uuid"),
        product=product,
        rating=rating,
        text=review_text,
        author=author,
        created_at=created_at,
        raw={
            **card,
            "helpful_yes": helpful_yes,
            "helpful_no": helpful_no,
        },
    )


def _extract_author(lines: list[str]) -> str | None:
    # В текущем HTML:
    # 0: инициалы аватара
    # 1: имя пользователя
    if len(lines) >= 2:
        return lines[1]

    return lines[0] if lines else None


def _extract_date(lines: list[str]) -> str | None:
    for line in lines:
        if DATE_RE.fullmatch(line):
            return line

    return None


def _extract_review_text(
    *,
    lines: list[str],
    author: str | None,
    review_date: str | None,
) -> str | None:
    result: list[str] = []

    for line in lines:
        if line == author:
            continue

        if line == review_date:
            continue

        if line == "Вам помог этот отзыв?":
            continue

        if line in {"Да", "Нет"}:
            continue

        if re.fullmatch(r"Да\s+\d+\s+Нет\s+\d+", line):
            continue

        # Первая строка — инициалы аватара.
        if len(lines) >= 2 and line == lines[0]:
            continue

        result.append(line)

    return "\n".join(result) or None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None

    return datetime.fromtimestamp(
        timestamp,
        tz=UTC,
    )