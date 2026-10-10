"""Avito reviews: discover the site's cursor, then browser fetch."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from infrastructure.transports.browser_api import (
    BrowserApiError,
    ResponseCapture,
    fetch_json,
    same_endpoint_url,
)
from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
from shared.url_parsers import extract_avito_profile_id

_SNAPSHOT = """
() => {
    const text = el => el?.textContent?.trim() || null;
    const marker = name => document.querySelector(`[data-marker="${name}"]`);
    const cards = [...document.querySelectorAll('[itemtype$="/Review"]')]
        .filter(el => /^review\\(\\d+\\)$/.test(el.dataset.marker || ''))
        .map(el => {
            const prefix = el.dataset.marker;
            const part = suffix =>
                el.querySelector(`[data-marker="${prefix}/${suffix}"]`);
            return {
                title: text(part('header/title')),
                rated: text(part('header/subtitle')),
                score: Number(el.querySelector('[itemprop="ratingValue"]')
                    ?.getAttribute('content')) || null,
                textSections: [...el.querySelectorAll(
                    '[data-marker$="/text-section/text"]'
                )].filter(x => !x.closest('[data-marker$="/answer"]'))
                    .map(x => ({text: text(x)})),
                answer: {textSections: [...(part('answer')
                    ?.querySelectorAll('[data-marker$="/text-section/text"]')
                    || [])].map(x => ({text: text(x)}))},
                images: [...el.querySelectorAll('img')]
                    .filter(x => !/avatar/i.test(x.className))
                    .map(x => ({url: x.src})),
                dom: true,
            };
        });
    const totalText = text(marker('ratingSummary/description')) || '';
    return {
        cards,
        total: Number(totalText.match(/[\\d\\s ]+/)?.[0]
            .replace(/\\s/g, '')) || null,
        average: Number(text(marker('ratingSummary/rating'))
            ?.replace(',', '.')) || null,
        title: text(document.querySelector('h1')),
    };
}
"""
_MORE = '[data-marker="rating-list/moreReviewsButton"]'


def is_ratings_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https" and parts.netloc == "www.avito.ru"
        and parts.path.startswith("/web/7/user/")
        and parts.path.endswith("/ratings")
    )


def rating_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise BrowserApiError("Avito API returned no entries array")
    return [
        entry["value"] for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "rating"
        and isinstance(entry.get("value"), dict)
        and entry["value"].get("id") is not None
    ]


class AvitoBrowserTransport:
    def __init__(
        self, *, timeout_ms: int = 90_000, settle_ms: int = 750,
        proxy: dict[str, str] | None = None,
        cookies: list[dict[str, Any]] | None = None,
        humanize: bool = True, use_api: bool = True,
        max_pages: int | None = None, page_delay_seconds: float = 0.35,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.proxy = proxy
        self.cookies = cookies
        self.humanize = humanize
        self.use_api = use_api
        self.max_pages = max_pages
        self.page_delay_seconds = page_delay_seconds
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        self.last_product_title: str | None = None
        self.incomplete_reason: str | None = None
        self.collection_path = "dom"
        self.api_fallback_reason: str | None = None

    async def iter_review_batches(
        self, url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        extract_avito_profile_id(url)
        async with import_invisible_playwright()(
            proxy=self.proxy, humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()
            if self.cookies:
                await page.context.add_cookies(self.cookies)
            with ResponseCapture(page, is_ratings_url) as capture:
                await page.goto(
                    url, wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                await page.locator('[data-marker="ratingSummary"]').wait_for(
                    state="attached", timeout=self.timeout_ms,
                )
                snapshot = await page.evaluate(_SNAPSHOT)
                self.last_total_count = snapshot.get("total")
                self.last_average_rating = snapshot.get("average")
                self.last_product_title = snapshot.get("title")
                first_url = None
                more = page.locator(_MORE).first
                if self.use_api and await more.count():
                    try:
                        await self._click_more(more)
                        deadline = asyncio.get_running_loop().time() + 8
                        while not capture.responses:
                            if asyncio.get_running_loop().time() >= deadline:
                                break
                            await asyncio.sleep(0.1)
                    except Exception:
                        pass  # DOM batch is still available.
                    if capture.responses:
                        first_url = capture.responses[-1].url
                if first_url:
                    parts = urlsplit(first_url)
                    query = dict(parse_qsl(parts.query))
                    query["offset"] = "0"
                    current = urlunsplit(
                        parts._replace(query=urlencode(query)),
                    )
                    seen_paths: set[str] = set()
                    seen_ids: set[str] = set()
                    page_number = 0
                    while current:
                        if current in seen_paths:
                            raise BrowserApiError("Avito repeated nextPage")
                        seen_paths.add(current)
                        try:
                            cached = next((
                                r for r in capture.responses
                                if r.url == current and r.status == 200
                            ), None)
                            payload = (
                                await cached.json() if cached else
                                await fetch_json(
                                    page, current, timeout_ms=self.timeout_ms,
                                )
                            )
                            cards = rating_entries(payload)
                        except Exception as exc:
                            if page_number:
                                raise
                            self.api_fallback_reason = (
                                str(exc) if isinstance(exc, BrowserApiError)
                                else type(exc).__name__
                            )
                            break  # No API rows emitted: safe DOM fallback.
                        self.collection_path = "api"
                        page_number += 1
                        fresh = []
                        for card in cards:
                            key = str(card["id"])
                            if key not in seen_ids:
                                seen_ids.add(key)
                                fresh.append(card)
                        if fresh:
                            yield fresh
                        next_page = payload.get("nextPage")
                        if next_page is None:
                            return
                        if not fresh:
                            self.incomplete_reason = "Avito API repeated cards"
                            return
                        if (
                            self.max_pages is not None
                            and page_number >= self.max_pages
                        ):
                            self.incomplete_reason = "Avito max_pages reached"
                            return
                        if not isinstance(next_page, str) or not next_page:
                            raise BrowserApiError("Invalid Avito nextPage")
                        current = same_endpoint_url(first_url, next_page)
                        await asyncio.sleep(self.page_delay_seconds)
                # Either the whole list fits on the page or discovery failed.
                if self.use_api and not first_url and await more.count():
                    self.api_fallback_reason = "review API was not observed"
                self.collection_path = "dom"
                from infrastructure.marketplaces.avito import avito_card_key

                seen: set[str] = set()
                for number in range(self.max_pages or 500):
                    snapshot = await page.evaluate(_SNAPSHOT)
                    fresh = []
                    for card in snapshot.get("cards", []):
                        key = avito_card_key(card)
                        if key not in seen:
                            seen.add(key)
                            fresh.append(card)
                    if fresh:
                        yield fresh
                    if not await more.count():
                        return
                    if number > 0 and not fresh:
                        self.incomplete_reason = "Avito DOM stalled"
                        return
                    await self._click_more(more)
                    await page.wait_for_timeout(max(500, self.settle_ms))
                self.incomplete_reason = "Avito DOM page limit"

    async def _click_more(self, locator: Any) -> None:
        """Read-only pagination button; survive mouse-driver errors."""
        try:
            await locator.click(timeout=min(self.timeout_ms, 5_000))
        except Exception:
            # Some Invisible Playwright builds lose mouse dispatch even
            # though the page and its React handler remain healthy.
            await locator.evaluate("(element) => element.click()")
