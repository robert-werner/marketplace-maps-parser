# src/infrastructure/transports/two_gis_browser.py
"""2GIS: one reviews navigation, hydrated state first, observed API second.

Never discard populated SSR reviews in favor of an empty API response.
Only a URL captured for the requested branch may supply the API key/cursor.
An unresolved hasMore/total mismatch remains partial, not a successful EOF.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from infrastructure.transports.browser_api import (
    ResponseCapture,
    fetch_json,
)
from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
from shared.url_parsers import extract_2gis_firm_path

TWO_GIS_BASE = "https://2gis.ru"

_REACT_STATE_JS = """
() => {
    const state = window.__REACT_QUERY_STATE__;
    return state ? JSON.stringify(state) : null;
}
"""


class TwoGisStateError(RuntimeError):
    """The reviews page rendered without the React-Query state
    (blocked, bot challenge or a layout change)."""


def find_entity_reviews_query(
    state: Any,
) -> dict[str, Any] | None:
    """The ``fetchEntityReviews`` query dict from the dehydrated
    React-Query cache."""
    if not isinstance(state, dict):
        return None
    for query in state.get("queries") or []:
        if not isinstance(query, dict):
            continue
        key = query.get("queryKey")
        if (
            isinstance(key, list)
            and key
            and key[0] == "fetchEntityReviews"
        ):
            return query
    return None


def extract_2gis_reviews(
    state: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(review cards, page meta) from the dehydrated state.

    ``meta`` carries ``total`` / ``rating`` / ``hasMore``; an empty
    result with a live page means the query was not hydrated.
    """
    query = find_entity_reviews_query(state)
    if query is None:
        return [], {}
    data = (query.get("state") or {}).get("data") or {}
    cards: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for page in data.get("pages") or []:
        if not isinstance(page, dict):
            continue
        meta = {
            key: page.get(key)
            for key in (
                "total",
                "totalForRequest",
                "rating",
                "orgRating",
                "hasMore",
            )
            if key in page
        }
        for card in page.get("items") or []:
            if isinstance(card, dict) and card.get("id"):
                cards.append(card)
    return cards, meta


class TwoGisBrowserTransport:
    """Yield SSR reviews and, if needed, browser API continuation batches."""

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 4_000,
        debug_dir: str | Path = "debug_2gis",
        proxy: dict[str, str] | None = None,
        proxy_pool: Any = None,
        cookies: list[dict[str, Any]] | None = None,
        humanize: bool = True,
        use_api: bool = True,
        max_pages: int | None = None,
        page_delay_seconds: float = 0.35,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.proxy_pool = proxy_pool
        self.cookies = cookies
        self.humanize = humanize
        self.use_api = use_api
        self.max_pages = max_pages
        self.page_delay_seconds = page_delay_seconds
        self.collection_path = "ssr"
        #: Filled while iterating (the CLI prints them in the
        #: summary).
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        #: Firm name (the unified output's ``product_title``) —
        #: taken from the org's own answer signature, present
        #: whenever at least one review got an official reply.
        self.last_product_title: str | None = None
        self.incomplete_reason: str | None = None

    async def iter_review_batches(
        self,
        firm_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Load ``/tab/reviews`` once and yield all SSR reviews."""
        import json

        firm_path = extract_2gis_firm_path(firm_url)

        browser_cls = import_invisible_playwright()
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        proxy = self.proxy
        if proxy is None and self.proxy_pool is not None:
            proxy = await self._next_proxy()

        async with browser_cls(
            proxy=proxy,
            seed=None,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()
            if self.cookies:
                try:
                    await page.context.add_cookies(self.cookies)
                except Exception as exc:
                    print(
                        f"2ГИС: не удалось внедрить cookies: {exc}"
                    )

            branch_id = firm_path.rsplit("/", 1)[-1]

            def is_reviews_response(url: str) -> bool:
                parts = urlsplit(url)
                return (
                    parts.scheme == "https"
                    and parts.netloc == "public-api.reviews.2gis.com"
                    and parts.path == f"/3.0/branches/{branch_id}/reviews"
                )

            with ResponseCapture(page, is_reviews_response) as capture:
                await page.goto(
                    f"{TWO_GIS_BASE}{firm_path}/tab/reviews",
                    timeout=self.timeout_ms,
                    referer=f"{TWO_GIS_BASE}{firm_path}",
                    wait_until="domcontentloaded",
                )
                await self._wait_for_state(page)

                state_text = await page.evaluate(_REACT_STATE_JS)
                state: Any = None
                if state_text:
                    try:
                        state = json.loads(state_text)
                    except json.JSONDecodeError:
                        state = None

                if state is None:
                    try:
                        html = await page.content()
                        (self.debug_dir / "no_state.html").write_text(
                            html, encoding="utf-8",
                        )
                    except Exception:
                        pass
                    raise TwoGisStateError(
                        "2ГИС: страница без __REACT_QUERY_STATE__ — "
                        "блокировка или смена разметки"
                    )

                cards, meta = extract_2gis_reviews(state)
                if find_entity_reviews_query(state) is None:
                    raise TwoGisStateError(
                        "2GIS: hydrated state contains no reviews query"
                    )
                total = meta.get("total")
                if isinstance(total, int):
                    self.last_total_count = total
                rating = meta.get("rating")
                if isinstance(rating, (int, float)):
                    self.last_average_rating = round(float(rating), 2)
                if self.last_product_title is None:
                    for card in cards:
                        answer = card.get("official_answer")
                        org_name = (
                            answer.get("org_name")
                            if isinstance(answer, dict)
                            else None
                        )
                        if isinstance(org_name, str) and org_name.strip():
                            self.last_product_title = org_name.strip()
                            break

                seen = {
                    str(card["id"]) for card in cards if card.get("id")
                }
                if cards:
                    yield cards
                total_count = self.last_total_count
                has_more = bool(meta.get("hasMore"))
                if has_more or (
                    total_count is not None and len(cards) < total_count
                ):
                    if self.use_api and capture.responses:
                        template = capture.responses[-1].url
                        parts = urlsplit(template)
                        query = dict(parse_qsl(parts.query))
                        offset = 0
                        for _ in range(self.max_pages or 200):
                            query["offset"] = str(offset)
                            api_url = urlunsplit(
                                parts._replace(query=urlencode(query)),
                            )
                            payload = await fetch_json(
                                page, api_url, timeout_ms=self.timeout_ms,
                            )
                            api_cards = payload.get("reviews")
                            if not isinstance(api_cards, list):
                                raise TwoGisStateError(
                                    "2GIS API has no reviews array"
                                )
                            fresh = []
                            for card in api_cards:
                                if not isinstance(card, dict):
                                    continue
                                key = card.get("id")
                                if key is not None and str(key) not in seen:
                                    seen.add(str(key))
                                    fresh.append(card)
                            if fresh:
                                self.collection_path = "ssr+api"
                                yield fresh
                            if total_count is not None and (
                                len(seen) >= total_count
                            ):
                                return
                            if not api_cards or (offset > 0 and not fresh):
                                break
                            offset += len(api_cards)
                            await asyncio.sleep(self.page_delay_seconds)
                        self.incomplete_reason = (
                            "2GIS API page limit or exhausted before total"
                        )
                        return
                    self.incomplete_reason = (
                        "2GIS SSR window incomplete; API unavailable/disabled"
                    )
                    print(
                        f"2ГИС: предупреждение — SSR отдал "
                        f"{len(cards)} из {total_count} отзывов"
                        + (
                            " (hasMore=true: API недоступен)"
                            if has_more
                            else ""
                        ),
                    )

    async def _next_proxy(self) -> dict[str, str] | None:
        """Pull one proxy for the session (async-aware)."""
        if self.proxy_pool is None:
            return None
        try:
            pool_next = getattr(self.proxy_pool, "next_async", None)
            if pool_next is not None:
                return cast(
                    "dict[str, str] | None",
                    await pool_next(),
                )
            return cast(
                "dict[str, str] | None",
                self.proxy_pool.next(),
            )
        except Exception:
            return None

    async def _wait_for_state(self, page: Any) -> None:
        """Return when SSR state is ready instead of sleeping blindly."""
        wait_for_function = getattr(page, "wait_for_function", None)
        if wait_for_function is not None:
            try:
                handle = await wait_for_function(
                    """() => window.__REACT_QUERY_STATE__?.queries?.some(
                        q => q.queryKey?.[0] === 'fetchEntityReviews'
                    )""",
                    timeout=min(self.timeout_ms, 10_000),
                )
                await handle.dispose()
                return
            except Exception:
                pass
        if self.settle_ms > 0:
            await page.wait_for_timeout(self.settle_ms)


__all__ = [
    "TwoGisBrowserTransport",
    "TwoGisStateError",
    "extract_2gis_reviews",
    "find_entity_reviews_query",
]
