"""Public-page transport for fetching Ozon reviews via the visible
review page DOM.

Production motivation: all the existing transports (``playwright``,
``curl_cffi``, ``hybrid``) hit Ozon's internal API endpoint
``/api/entrypoint-api.bx/page/json/v2``, which is heavily protected
by Cloudflare. Even with TLS-fingerprint impersonation, stealth
init scripts, and JS-challenge auto-resolution, Cloudflare still
returns 403 challenge bodies on a significant fraction of requests,
requiring many retries and ultimately failing on some products.

This transport takes a completely different approach: it loads the
**public review page** (``https://www.ozon.ru/product/<id>/reviews
?page=N``) directly in the browser, then reads review cards from
the DOM via the ``[data-review-uuid]`` selector. The public review
page is served by Ozon's CDN, not the API endpoint, and is much
less aggressively protected by Cloudflare — ordinary browser
navigation typically passes through without any challenge.

The transport implements the same ``OzonBrowserTransport`` Protocol
as the other transports, so ``OzonAdapter`` accepts it via the
``browser_transport`` constructor arg.

Pagination strategy: visit ``/reviews?page=1``, parse all
``[data-review-uuid]`` cards, then increment the page counter and
load ``/reviews?page=2``, etc. Stop when a page returns 0 new
reviews OR the page contains a "no more reviews" sentinel OR after
a configurable max-pages cap.

Scroll strategy: same page URL, but instead of paginating via
``?page=N``, scroll to the bottom of the page to trigger lazy-load
of additional review cards. Stop after ``max_idle_rounds``
consecutive scrolls with no new reviews.

Both strategies share the same DOM-card reader
(``_read_review_card``) and deduplication logic (by ``uuid``).
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from infrastructure.transports.base import (
    OZON_BASE_URL,
    OzonTransportMixin,
)


def _import_invisible_playwright():
    """Lazy import of invisible-playwright (same pattern as
    browser_json.py). Wrapped with GPU-safe software-rendering
    prefs (see transports/gpu_safety.py)."""
    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )
    return import_invisible_playwright()


def _import_retry_async():
    from shared.retry import retry_async
    return retry_async


def _import_sleep_with_jitter():
    from shared.retry import sleep_with_jitter
    return sleep_with_jitter


def _import_retryable_errors():
    """Reuse the retryable-errors factory from browser_common so all
    transports share the same retry semantics for Playwright errors
    (returns the callable; callers invoke it lazily)."""
    from infrastructure.transports.browser_common import (
        _retryable_errors,
    )
    return _retryable_errors


# Default user agent — used for logging only; invisible-playwright
# already sets a real browser UA.



from infrastructure.transports.public_page._dom import CardReadingMixin
from infrastructure.transports.public_page._nav import NavigationMixin
from infrastructure.transports.public_page._pagination import PaginationMixin
from infrastructure.transports.public_page._widget import WidgetFlowMixin


class PublicPageTransport(
    WidgetFlowMixin,
    PaginationMixin,
    NavigationMixin,
    CardReadingMixin,
    OzonTransportMixin,
):
    """Ozon reviews transport that scrapes the public review page
    DOM instead of the internal API.

    Two strategies:

    - ``iter_ozon_reviews_json`` — paginate via ``?page=N``. Yields
      ``(page_num, payload)`` tuples where ``payload`` is a dict
      with a ``reviews`` key (a list of card dicts), so it can be
      consumed by the same adapter code that consumes the API
      payload. The adapter's ``extract_reviews_from_ozon_payload``
      walks the payload recursively, so this works.

    - ``iter_ozon_reviews_by_scroll`` — scroll the review page to
      trigger lazy-load of more cards. Yields batches of new cards.

    - ``iter_all_ozon_reviews`` — runs pagination first, then scroll
      as a supplement. Yields ``(strategy, node)`` tuples.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 3_000,
        debug_dir: str = "debug_ozon_public",
        proxy: dict[str, str] | None = None,
        proxy_pool: Any | None = None,
        seed: int | None = None,
        pin: dict[str, Any] | None = None,
        humanize: bool = True,
        stealth: bool = True,
        max_idle_pages: int = 2,
        scroll_max_idle_rounds: int = 5,
        scroll_step: int = 1800,
        scroll_pause_ms: int = 800,
        randomize_fingerprint: bool = False,
        warmup: bool = True,
        # Playwright-format cookies of a logged-in Ozon session
        # (see cookie_loader.load_cookies_file). Injected into
        # every page — unlocks the full review list; anonymous
        # sessions cap at ~33 widget pages (measured 2026-09-15).
        cookies: list[dict[str, Any]] | None = None,
        # How long to wait for review cards to attach. Measured
        # 2026-09-15: on slow proxy sessions Ozon's reviews widget
        # renders later than 30 s — a premature "no cards" verdict
        # discarded pages that were perfectly fine (the full-page
        # screenshot taken seconds later showed all 30 cards).
        card_wait_ms: int = 90_000,
        # Parallel widget-flow workers: browser TABS in the SAME
        # session, each walking its own segment of review pages
        # (the widget's page_key URLs are directly addressable).
        # 1 = current sequential behavior.
        workers: int = 1,
        # Scroll-mix in the widget flow: after reading a page,
        # scroll it like a reader and pick up any cards the widget
        # lazy-appends, THEN go to the next page (whether or not
        # anything was appended). Disable with --no-widget-scroll.
        widget_scroll: bool = True,
        # How long to wait for lazy-appended cards after scrolling
        # (0 = scroll but don't wait).
        lazy_wait_ms: int = 1_500,
        # Abort image/font/media requests on scraper pages: the
        # reviews page is ~880KB and most of it is review photos we
        # never look at (the src urls stay in the DOM). Disable
        # with --no-block-assets.
        block_assets: bool = True,
        # Save a page screenshot into the debug dir on every debug
        # dump. OFF by default: full-page screenshots of a logged-in
        # session are a PII hazard and cost a noticeable share of
        # the ~8s/page wall time. HTML/cards dumps stay on.
        screenshots: bool = False,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        # Proxy pool takes precedence over single proxy. When a
        # proxy_pool is provided, each page fetch rotates to the
        # next available proxy — this is the primary defense against
        # Cloudflare IP-based blocking ("Выключите VPN" pages).
        self.proxy_pool = proxy_pool
        self.proxy = proxy if proxy_pool is None else None
        self.seed = seed
        self.pin = pin
        self.humanize = humanize
        self.stealth = stealth
        # Stop pagination after this many consecutive pages with
        # 0 new reviews. Guards against "soft 404" pages where Ozon
        # returns the same content for high page numbers.
        self.max_idle_pages = max_idle_pages
        # Scroll-mode tuning.
        self.scroll_max_idle_rounds = scroll_max_idle_rounds
        self.scroll_step = scroll_step
        self.scroll_pause_ms = scroll_pause_ms
        # When True, create a fresh InvisiblePlaywright browser for
        # each page instead of reusing one for the whole pagination
        # run. Each new browser gets a new random fingerprint (when
        # seed=None), so every page looks like a different browser
        # to Cloudflare. Slower (~2-5s browser startup per page) but
        # maximally stealthy.
        self.randomize_fingerprint = randomize_fingerprint
        # Antibot hardening: before hitting /reviews directly, land
        # on the product page first (like a real visitor), let Ozon
        # set its antibot cookies, then navigate to the reviews with
        # a referer. Measured 2026-09: direct cold hits on
        # /reviews?page=N get "Antibot Challenge Page" /
        # «Похоже, нет соединения» far more often than warmed-up
        # navigations.
        self.warmup = warmup
        self.cookies = cookies
        self.card_wait_ms = card_wait_ms
        self.workers = max(1, workers)
        self.widget_scroll = widget_scroll
        self.lazy_wait_ms = lazy_wait_ms
        self.block_assets = block_assets
        self.screenshots = screenshots

    # ------------------------------------------------------------------
    # Scroll iterator
    # ------------------------------------------------------------------
    async def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Scroll the public review page to load all reviews.

        Yields batches of new review cards as they appear during
        scrolling. Stops after ``scroll_max_idle_rounds``
        consecutive scrolls with no new cards.
        """
        scroll_proxy = await self._get_proxy_for_page()
        if scroll_proxy is not None and self.proxy_pool is not None:
            print(
                "Ozon (public scroll): proxy: "
                f"{scroll_proxy.get('server', 'unknown')}"
            )
        async with _import_invisible_playwright()(
            proxy=scroll_proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await self._new_page_with_stealth(browser)

            reviews_url = (
                f"{OZON_BASE_URL}{product_path}/reviews?page=1"
            )

            # Same human-like navigation as pagination: warm up on
            # the product page, then open the reviews with referer.
            await self._goto_reviews_like_human(
                page,
                product_path=product_path,
                reviews_url=reviews_url,
                referer_url=self._absolute_url(product_path),
                retry_async=_import_retry_async(),
                retryable_errors=_import_retryable_errors(),
                label="Ozon public scroll",
            )

            if self.settle_ms > 0:
                await page.wait_for_timeout(self.settle_ms)

            review_locator = page.locator("[data-review-uuid]")
            try:
                await review_locator.first.wait_for(
                    state="attached",
                    timeout=self.card_wait_ms,
                )
            except Exception as exc:
                antibot = await self._page_is_antibot(page)
                if antibot:
                    self._mark_proxy_blocked(scroll_proxy)
                raise RuntimeError(
                    "Ozon (public scroll): no [data-review-uuid] "
                    f"cards found on {reviews_url}"
                    + (" (antibot/challenge page)" if antibot else "")
                    + f": {exc}"
                ) from exc

            seen_uuids: set[str] = set()
            idle_rounds = 0

            for _round_num in range(1, 1000):
                cards = await self._read_review_cards(review_locator)

                new_cards = []
                for card in cards:
                    uuid = card.get("uuid")
                    if uuid and uuid in seen_uuids:
                        continue
                    if uuid:
                        seen_uuids.add(uuid)
                    new_cards.append(card)

                if new_cards:
                    idle_rounds = 0
                    yield new_cards

                if (
                    max_reviews is not None
                    and len(seen_uuids) >= max_reviews
                ):
                    return

                if idle_rounds >= self.scroll_max_idle_rounds:
                    print(
                        f"Ozon (public scroll): {idle_rounds} "
                        "подряд пустых scroll-rounds; сбор завершён"
                    )
                    return

                before_count = await review_locator.count()
                await page.mouse.wheel(0, self.scroll_step)
                await page.wait_for_timeout(self.scroll_pause_ms)
                after_count = await review_locator.count()

                if after_count <= before_count:
                    # Lazy-load might still be in flight; give it
                    # one more chance.
                    await asyncio.sleep(2)
                    final_count = await review_locator.count()
                    if final_count <= before_count:
                        idle_rounds += 1
                else:
                    idle_rounds = 0

    # ------------------------------------------------------------------
    # Unified iterator — pagination + scroll supplement
    # ------------------------------------------------------------------
    async def iter_all_ozon_reviews(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
        pagination_max_pages: int | None = None,
        pagination_start_page: int = 1,
        scroll_max_rounds: int = 500,
        page_delay_seconds: float = 1.5,
        scroll_pause_seconds: float = 1.0,
        retry_attempts: int = 3,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run the widget flow (deep pagination via the reviews
        widget), then URL pagination + scroll as fallback phases.

        Yields ``(strategy, node)`` tuples. ``strategy`` is
        ``"widget"``, ``"pagination"`` or ``"scroll"``.
        """
        from shared.pacing import AdaptivePacer
        from shared.retry import sleep_with_jitter

        seen_ids: set[str] = set()

        # --- widget flow (primary) ---
        # Deep pagination through the widget's own «Дальше» control;
        # when it works it covers everything the URL-pagination and
        # scroll phases could reach, and much more beyond (see
        # ``iter_ozon_reviews_by_widget``). The legacy phases only
        # run when the widget flow produced nothing.
        try:
            async for batch in self.iter_ozon_reviews_by_widget(
                product_path=product_path,
                max_reviews=max_reviews,
                retry_attempts=retry_attempts,
            ):
                for card in batch:
                    rid = card.get("uuid")
                    if rid and rid in seen_ids:
                        continue
                    if rid:
                        seen_ids.add(rid)
                    yield "widget", card
                    if (
                        max_reviews is not None
                        and len(seen_ids) >= max_reviews
                    ):
                        return
        except Exception as exc:
            print(
                "Ozon (public auto): widget phase failed: "
                f"{exc} — пробую классическую пагинацию"
            )

        if seen_ids:
            # The widget flow already covered the first pages; URL
            # pagination and scroll can only re-deliver the same
            # ~150 anonymous-capped reviews.
            return

        if max_reviews is not None and len(seen_ids) >= max_reviews:
            return

        # --- pagination (fallback) ---
        # The browser launch includes an egress-IP check through the
        # proxy; paid rotating pools (e.g. proxys.io) intermittently
        # answer 503 to CONNECT. Retry the phase — the pool rotates to
        # another exit IP on each attempt.
        # Adaptive inter-page pacing: starts at page_delay_seconds,
        # shrinks after clean pages, backs off on antibot events
        # (see _notify_pacer_block). None when pacing is disabled.
        self._pacer: AdaptivePacer | None = (
            AdaptivePacer(base_delay=page_delay_seconds)
            if page_delay_seconds > 0
            else None
        )

        pagination_attempts = max(3, retry_attempts)
        try:
            for attempt in range(1, pagination_attempts + 1):
                try:
                    async for _page_num, payload in (
                        self.iter_ozon_reviews_json(
                            product_path=product_path,
                            start_page=pagination_start_page,
                            max_pages=pagination_max_pages,
                            retry_attempts=retry_attempts,
                        )
                    ):
                        for node in payload.get("reviews", []):
                            rid = node.get("reviewId") or (
                                node.get("uuid")
                            )
                            if rid and rid in seen_ids:
                                continue
                            if rid:
                                seen_ids.add(rid)

                            yield "pagination", node

                            if (
                                max_reviews is not None
                                and len(seen_ids) >= max_reviews
                            ):
                                return

                        if self._pacer is not None:
                            self._pacer.record_success()
                            await self._pacer.wait()
                    break
                except Exception as exc:
                    if attempt >= pagination_attempts:
                        print(
                            "Ozon (public auto): pagination phase "
                            f"failed after {attempt} attempts: {exc}"
                        )
                    else:
                        print(
                            "Ozon (public auto): pagination attempt "
                            f"{attempt}/{pagination_attempts} "
                            f"failed: {exc} — retry with next proxy"
                        )
                        await sleep_with_jitter(5.0)
        finally:
            self._pacer = None

        if max_reviews is not None and len(seen_ids) >= max_reviews:
            return

        # --- scroll supplement ---
        # The scroll phase opens its own browser and pulls the next
        # proxy from the pool, so retrying it after an antibot
        # challenge automatically rotates to a different exit IP.
        scroll_attempts = max(2, min(retry_attempts, 5))
        for attempt in range(1, scroll_attempts + 1):
            try:
                async for batch in self.iter_ozon_reviews_by_scroll(
                    product_path=product_path,
                    max_reviews=None,
                ):
                    for card in batch:
                        rid = card.get("uuid")
                        if rid and rid in seen_ids:
                            continue
                        if rid:
                            seen_ids.add(rid)

                        # Wrap the DOM card in a shape that the
                        # adapter's map_ozon_review_node /
                        # parse_ozon_dom_card can consume.
                        node = {
                            "reviewId": card.get("uuid"),
                            "rating": card.get("rating"),
                            "text": card.get("text"),
                            "author": card.get("author"),
                            "published_at": card.get("published_at"),
                        }
                        yield "scroll", node

                        if (
                            max_reviews is not None
                            and len(seen_ids) >= max_reviews
                        ):
                            return

                    if scroll_pause_seconds > 0:
                        await sleep_with_jitter(scroll_pause_seconds)
                break
            except Exception as exc:
                if attempt >= scroll_attempts:
                    print(
                        "Ozon (public auto): scroll phase failed "
                        f"after {attempt} attempts: {exc}"
                    )
                else:
                    print(
                        "Ozon (public auto): scroll attempt "
                        f"{attempt}/{scroll_attempts} failed: "
                        f"{exc} — retry with next proxy"
                    )
                    await sleep_with_jitter(5.0)

    # ------------------------------------------------------------------
    # Single-page fetch (mirrors the Protocol's
    # get_ozon_reviews_json)
    # ------------------------------------------------------------------
    async def get_ozon_reviews_json(
        self,
        product_path: str,
        *,
        page_number: int = 1,
    ) -> dict[str, Any]:
        """Fetch a single page of reviews.

        Used by ``OzonAdapter.collect`` (single-page mode).
        """
        async for _current_page, payload in self.iter_ozon_reviews_json(
            product_path=product_path,
            start_page=page_number,
            max_pages=1,
        ):
            return payload
        raise RuntimeError(
            f"Ozon (public): не удалось получить страницу {page_number}"
        )

    async def _get_proxy_for_page(self) -> dict[str, str] | None:
        """Return the proxy to use for the next page fetch.

        When a ``proxy_pool`` is configured, returns the next proxy
        in the rotation (async — a ``FreeProxyPool`` refill fetches
        a new batch off the event loop). When ``proxy`` is
        configured (single proxy), returns that. When neither is
        configured, returns ``None`` (direct connection).
        """
        if self.proxy_pool is not None:
            pool = self.proxy_pool
            next_async = getattr(pool, "next_async", None)
            if next_async is not None:
                proxy = await next_async()
            else:
                proxy = pool.next()
            if proxy is None:
                print(
                    "Ozon (public): WARNING — все proxy в пуле "
                    "заблокированы! Использую прямое подключение."
                )
                return None
            return proxy
        return self.proxy

    def _mark_proxy_blocked(self, proxy: dict[str, str] | None) -> None:
        """Mark a proxy as blocked in the pool (if a pool is active).

        Called when a page fetch returns a Cloudflare "Выключите VPN"
        block page — the proxy's IP is on Cloudflare's blocklist and
        should not be reused.
        """
        if proxy is None or self.proxy_pool is None:
            return
        self.proxy_pool.mark_blocked(proxy)
        stats = self.proxy_pool.get_stats()
        print(
            f"Ozon (public): proxy заблокирован "
            f"({proxy.get('server', 'unknown')}). "
            f"Доступно proxy: {stats['available']}/{stats['total']}"
        )

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------
    async def _save_debug_page(
        self,
        *,
        page,
        page_number: int,
        cards: list[dict[str, Any]] | None = None,
    ) -> None:
        """Save the parsed cards (+ optional screenshot) for
        postmortem."""
        page_dir = self.debug_dir / f"page_{page_number}"
        page_dir.mkdir(parents=True, exist_ok=True)

        if self.screenshots:
            try:
                await page.screenshot(
                    path=str(page_dir / "page.png"),
                    full_page=True,
                )
            except Exception:
                pass

        if cards is not None:
            (page_dir / "cards.json").write_text(
                json.dumps(
                    cards,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """No persistent resources — invisible-playwright sessions
        are scoped to each iterator call (``async with``). This
        method exists for API compatibility with the
        ``OzonBrowserTransport`` Protocol.
        """
        pass
