"""WB fast path: consume review JSON actually requested by the page."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from infrastructure.transports.browser_api import (
    BrowserApiError,
    fetch_json,
    same_endpoint_url,
)


def is_feedback_url(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and (host.endswith(".wb.ru") or host.endswith(".wildberries.ru"))
        and "/feedback" in parts.path.lower()
    )


def feedback_cards(
    payload: dict[str, Any], nm_id: int,
) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    feedbacks = data.get("feedbacks")
    if not isinstance(feedbacks, list):
        raise BrowserApiError("WB response contains no feedbacks array")
    cards = []
    for node in feedbacks:
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        details = node.get("productDetails") or {}
        owner = node.get("nmId") or details.get("nmId")
        # The same page also requests recommendations/other variants.
        # Without a product binding we cannot safely attribute the review.
        if owner is None or str(owner) != str(nm_id):
            continue
        user = node.get("wbUserDetails") or {}
        answer = node.get("answer") or {}
        photos = []
        for photo in node.get("photoLinks") or []:
            if isinstance(photo, dict):
                link = photo.get("fullSize") or photo.get("miniSize")
                if isinstance(link, str):
                    photos.append(link)
        cards.append({
            "id": str(node["id"]), "api": True,
            "author": user.get("name") or node.get("userName"),
            "date": node.get("createdDate"),
            "rating": node.get("productValuation"),
            "text": node.get("text"), "pros": node.get("pros"),
            "cons": node.get("cons"), "photos": photos,
            "answer": answer.get("text") if isinstance(answer, dict) else None,
        })
    return cards


def feedback_next_url(url: str, payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or payload
    next_page = data.get("nextPage") or data.get("next")
    if isinstance(next_page, str):
        return same_endpoint_url(url, next_page)
    # Cursor pagination is used only when the response explicitly provides it.
    cursor = data.get("nextCursor")
    if isinstance(cursor, str) and cursor:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if "cursor" not in query:
            return None
        query["cursor"] = cursor
        return urlunsplit(parts._replace(query=urlencode(query)))
    return None


async def fetch_feedback_page(
    page: Any, url: str, *, timeout_ms: int,
) -> dict[str, Any]:
    if not is_feedback_url(url):
        raise BrowserApiError("Not a WB feedback endpoint")
    return await fetch_json(
        page, url, timeout_ms=timeout_ms, credentials="omit",
    )
