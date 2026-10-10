"""Read Market's structured review state without rendering every next page.

This is a browser fetch of the site's own reviews document, not a guessed
private resolver. APIary reviewList/ugcItems carry ratings and pagination
tokens together, unlike the older DOM + next-page JSON-LD merge.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from infrastructure.marketplaces.yandex import parse_yandex_date
from infrastructure.transports.browser_api import BrowserApiError

_EXTRACT = """
const extract = doc => {
    const patches = [...doc.querySelectorAll('noframes[data-apiary="patch"]')];
    for (const el of patches) {
        let data;
        try { data = JSON.parse(el.textContent); } catch { continue; }
        const list = data.collections?.reviewList;
        if (list && Array.isArray(list.ugcItems)) {
            const product = [...doc.querySelectorAll(
                'script[type="application/ld+json"]'
            )].map(el => {try {return JSON.parse(el.textContent)} catch {}})
                .find(p => p?.['@type'] === 'Product');
            return {list, title: product?.name || null,
                dates: product?.review || []};
        }
    }
    return null;
};
"""
_CURRENT = "() => {" + _EXTRACT + "return extract(document);}"
_FETCH = """
async ({url, timeoutMs}) => {
""" + _EXTRACT + """
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            credentials: 'include', signal: controller.signal,
            headers: {Accept: 'text/html'},
        });
        if (!response.ok) return {status: response.status};
        const html = await response.text();
        return {status: response.status,
            data: extract(new DOMParser().parseFromString(html, 'text/html'))};
    } finally { clearTimeout(timer); }
}
"""


def market_cards(
    data: dict[str, Any], product_id: str,
) -> list[dict[str, Any]]:
    """Map only this product's reviewV2 items; never feed/seller widgets."""
    reviews = (data.get("list") or {}).get("ugcItems")
    if not isinstance(reviews, list):
        raise BrowserApiError("Market reviewList missing")
    dates: dict[str, list[str]] = {}
    for row in data.get("dates") or []:
        if not isinstance(row, dict) or not isinstance(
            row.get("author"), dict,
        ):
            continue
        name = row["author"].get("name")
        published = row.get("datePublished")
        if isinstance(name, str) and isinstance(published, str):
            dates.setdefault(name, []).append(published)
    cards = []
    for item in reviews:
        node = item.get("reviewV2") if isinstance(item, dict) else None
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        params = (node.get("transition") or {}).get("params") or {}
        offer_id = params.get("oskuId")
        if offer_id is not None and str(offer_id) != product_id:
            continue
        author = (node.get("author") or {}).get("nickname")
        descriptor = node.get("descriptor") or []
        date = next((
            d.get("content") for d in descriptor
            if isinstance(d, dict) and d.get("type") == "text"
        ), None)
        if not parse_yandex_date(date):
            # JSON-LD can be shifted relative to the rendered list. Match
            # day/month before borrowing a year; never merge by author alone.
            candidates = (
                dates.get(author, []) if isinstance(author, str) else []
            )
            matching = []
            for candidate in candidates:
                parsed = parse_yandex_date(candidate)
                if parsed is not None and (
                    not date or parse_yandex_date(
                        f"{date} {parsed.year}",
                    ) == parsed
                ):
                    matching.append(candidate)
            if len(matching) == 1:
                date = matching[0]
        photos = []
        for media in node.get("media") or []:
            picture = media.get("picture") or {}
            if all(picture.get(k) for k in (
                "namespace", "groupId", "imageName",
            )):
                photos.append(
                    "https://avatars.mds.yandex.net/get-"
                    f"{picture['namespace']}/{picture['groupId']}/"
                    f"{picture['imageName']}/orig",
                )
        cards.append({
            "uuid": str(node["id"]), "author": author, "date": date,
            "rating": node.get("rating"), "text": node.get("comment"),
            "pros": node.get("pro"), "cons": node.get("contra"),
            "photos": photos, "offer_id": str(offer_id or product_id),
            "structured": True,
        })
    return cards


async def iter_market_state(
    transport: Any, page: Any, card_path: str, state: dict[str, Any],
) -> AsyncIterator[list[dict[str, Any]]]:
    url = f"https://market.yandex.ru{card_path}/reviews"
    number = state["page_no"]
    await page.goto(
        url + "?" + urlencode({"page": number}),
        wait_until="domcontentloaded", timeout=transport.timeout_ms,
    )
    landed = str(page.url)
    if "/showcaptcha" in landed or "/reviews" not in landed:
        raise BrowserApiError("Market reviews page unavailable")
    handle = await page.wait_for_function(
        """() => [...document.querySelectorAll(
            'noframes[data-apiary="patch"]'
        )].some(el => el.textContent.includes('"reviewList"'))""",
        timeout=min(transport.timeout_ms, 10_000),
    )
    await handle.dispose()
    data = await page.evaluate(_CURRENT)
    tokens: set[str] = set()
    while True:
        if not isinstance(data, dict):
            raise BrowserApiError("Market structured review state unavailable")
        listing = data.get("list")
        if not isinstance(listing, dict):
            raise BrowserApiError("Market reviewList missing")
        transport._update_totals({
            "total_count": (listing.get("reviewStats") or {}).get(
                "reviewsCount",
            ),
            "average_rating": (listing.get("ratingStats") or {}).get(
                "ratingValue",
            ),
            "product_name": data.get("title"),
        })
        batch: list[dict[str, Any]] = []
        product_id = card_path.rstrip("/").split("/")[-1]
        cards = market_cards(data, product_id)
        transport._absorb_new_cards(cards, state["seen"], batch)
        if batch:
            transport.collection_path = "browser_state_fetch"
            yield batch
        total = transport.last_total_count
        if total is not None and len(state["seen"]) >= total:
            return
        token = listing.get("nextPageToken")
        if not token:
            return
        if not batch:
            raise BrowserApiError("Market repeated review page")
        token = str(token)
        if token in tokens:
            raise BrowserApiError("Market repeated pagination token")
        tokens.add(token)
        number += 1
        state["page_no"] = number
        if (
            transport.max_pages is not None
            and number - transport.start_page >= transport.max_pages
        ):
            transport.incomplete_reason = "Market max_pages reached"
            return
        await asyncio.sleep(transport.page_delay_seconds)
        result = await page.evaluate(_FETCH, {
            "url": url + "?" + urlencode({"page": token}),
            "timeoutMs": transport.timeout_ms,
        })
        if not isinstance(result, dict) or result.get("status") != 200:
            raise BrowserApiError("Market page fetch failed")
        data = result.get("data")
