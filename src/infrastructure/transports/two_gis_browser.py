# src/infrastructure/transports/two_gis_browser.py
"""2GIS firm-reviews transport: one page load, SSR state harvest.

Measured 2026-09-19 against a live firm page (branch
70000001063192616, 22 reviews):

1. **SSR-only data** — the reviews tab
   (``/<city>/firm/<id>/tab/reviews``) ships the ENTIRE review list
   server-side inside ``window.__REACT_QUERY_STATE__`` (a dehydrated
   React-Query cache): the ``fetchEntityReviews`` query holds
   ``state.data.pages[].items`` (every review card incl.
   ``official_answer``, ``user``, ``date_created``, ``rating``,
   ``text``, ``likes_count``, ``emojis``, ``trust_factors``) plus
   the page meta (``total``, ``rating``, ``hasMore``).
2. **No usable public API** — ``public-api.reviews.2gis.com`` (the
   widget's list endpoint) answers ``total_count: 0`` even from the
   site's own runtime context; the ratings/summary/comments
   sub-endpoints work but do not list reviews. A cold curl_cffi
   session gets an ~11 KB shell without the state — the browser is
   required.
3. **One batch** — the transport loads the page once and yields a
   single batch; ``hasMore``/``total`` mismatches are surfaced (the
   SSR window has not been observed to cap, but a huge firm may
   paginate client-side — unimplemented, reported loudly).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

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
    """Yields ONE batch of raw 2GIS review cards per firm."""

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
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.proxy_pool = proxy_pool
        self.cookies = cookies
        self.humanize = humanize
        #: Filled while iterating (the CLI prints them in the
        #: summary).
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None

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

            await page.goto(
                f"{TWO_GIS_BASE}{firm_path}",
                timeout=self.timeout_ms,
            )
            await page.wait_for_timeout(self.settle_ms)

            await page.goto(
                f"{TWO_GIS_BASE}{firm_path}/tab/reviews",
                timeout=self.timeout_ms,
                referer=f"{TWO_GIS_BASE}{firm_path}",
            )
            await page.wait_for_timeout(self.settle_ms)

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
            total = meta.get("total")
            if isinstance(total, int):
                self.last_total_count = total
            rating = meta.get("rating")
            if isinstance(rating, (int, float)):
                self.last_average_rating = round(float(rating), 2)

            if cards:
                yield cards

            has_more = bool(meta.get("hasMore"))
            total_count = self.last_total_count
            if total_count is not None and len(cards) < total_count:
                print(
                    f"2ГИС: предупреждение — SSR отдал "
                    f"{len(cards)} из {total_count} отзывов"
                    + (
                        " (hasMore=true: клиентская пагинация не "
                        "реализована)"
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


__all__ = [
    "TwoGisBrowserTransport",
    "TwoGisStateError",
    "extract_2gis_reviews",
    "find_entity_reviews_query",
]
