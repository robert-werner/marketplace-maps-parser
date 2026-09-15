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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


def _import_invisible_playwright():
    """Lazy import of invisible-playwright (same pattern as
    browser_json.py)."""
    from invisible_playwright.async_api import InvisiblePlaywright
    return InvisiblePlaywright


def _import_retry_async():
    from shared.retry import retry_async
    return retry_async


def _import_sleep_with_jitter():
    from shared.retry import sleep_with_jitter
    return sleep_with_jitter


def _import_retryable_errors():
    """Reuse the retryable-errors tuple from browser_json so all
    transports share the same retry semantics for Playwright errors.
    """
    from infrastructure.transports.browser_json import (
        _retryable_errors,
    )
    return _retryable_errors


# Default user agent — used for logging only; invisible-playwright
# already sets a real browser UA.


class PublicPageTransport:
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
        # How long to wait for review cards to attach. Measured
        # 2026-09-15: on slow proxy sessions Ozon's reviews widget
        # renders later than 30 s — a premature "no cards" verdict
        # discarded pages that were perfectly fine (the full-page
        # screenshot taken seconds later showed all 30 cards).
        card_wait_ms: int = 90_000,
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
        self.card_wait_ms = card_wait_ms

    # ------------------------------------------------------------------
    # Antibot detection & human-like navigation
    # ------------------------------------------------------------------
    # Titles of Ozon/Cloudflare challenge and error pages. Real
    # review pages have titles like "304 отзыв на … OZON" — none of
    # these markers appear there.
    _ANTIBOT_TITLE_MARKERS = (
        "antibot",           # Ozon "Antibot Challenge Page"
        "нет соединения",    # Ozon «Похоже, нет соединения»
        "доступ ограничен",  # Ozon geo/ISP block page
        "cloudflare",        # Cloudflare block pages
        "just a moment",     # Cloudflare interstitial
        "attention required",
    )
    # HTML markers that NEVER appear on a real reviews page. NOTE:
    # the plain string "antibot" is NOT here — a real reviews page
    # contains it too (measured 2026-09: 6 occurrences in a page
    # that rendered 30 cards), so it cannot discriminate.
    _ANTIBOT_HTML_MARKERS = (
        "Выключите VPN",          # Cloudflare "Выключите VPN…" block
        "__cf_chl",               # Cloudflare challenge
        "cf-chl",                 # Cloudflare challenge (css/js)
        "challenge-platform",     # Cloudflare challenge script
    )

    async def _page_is_antibot(self, page) -> bool:
        """True when the loaded page is a challenge/block/error page
        rather than real content. Title first (cheap, reliable);
        fall back to unambiguous Cloudflare markers in the HTML."""
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        for marker in self._ANTIBOT_TITLE_MARKERS:
            if marker in title:
                return True
        try:
            html = await page.content()
        except Exception:
            return False
        return any(
            marker in html for marker in self._ANTIBOT_HTML_MARKERS
        )

    async def _warmup_goto(
        self,
        page,
        *,
        product_path: str,
        retry_async,
        retryable_errors,
        label: str,
    ) -> None:
        """Land on the product page so Ozon/Cloudflare set their
        cookies for this browser session, then pause like a human
        reading the product card. No-op when ``self.warmup`` is
        False; a failed warmup is logged and never kills the fetch."""
        if not self.warmup:
            return
        warmup_url = f"https://www.ozon.ru{product_path}"
        try:
            await retry_async(
                lambda: page.goto(
                    warmup_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                ),
                attempts=2,
                base_delay=2.0,
                max_delay=10.0,
                factor=2.0,
                jitter=0.3,
                retry_on=retryable_errors(),
                label=f"{label} warmup",
            )
            await page.wait_for_timeout(
                random.randint(800, 2_200),
            )
        except Exception as exc:
            print(
                f"Ozon (public): warmup не удался "
                f"({type(exc).__name__}) — иду напрямую на отзывы"
            )

    async def _goto_reviews_like_human(
        self,
        page,
        *,
        product_path: str,
        reviews_url: str,
        referer_url: str | None,
        retry_async,
        retryable_errors,
        label: str,
        goto_attempts: int = 3,
        do_warmup: bool = True,
    ) -> None:
        """Navigate to the reviews page the way a real visitor does:
        optionally warm up on the product page (``do_warmup`` — skip
        when the session was already warmed), then go to the reviews
        URL with an HTTP referer. A cold referer-less hit on
        /reviews is a strong bot signal."""
        if do_warmup:
            await self._warmup_goto(
                page,
                product_path=product_path,
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                label=label,
            )

        await retry_async(
            lambda: page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
                **({"referer": referer_url} if referer_url else {}),
            ),
            attempts=goto_attempts,
            base_delay=3.0,
            max_delay=30.0,
            factor=2.0,
            jitter=0.3,
            retry_on=retryable_errors(),
            label=label,
        )

    # ------------------------------------------------------------------
    # Pagination iterator (mirrors BrowserJsonTransport.iter_ozon_reviews_json)
    # ------------------------------------------------------------------
    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
        retry_attempts: int = 3,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Paginate the public review page via ``?page=N``.

        Yields ``(page_num, payload)`` tuples where ``payload`` is
        a dict shaped like the API response (with a ``reviews`` key
        and a ``nextPage`` key) so the existing adapter code can
        consume it unchanged.

        When ``randomize_fingerprint=True``, a fresh
        ``InvisiblePlaywright`` browser is created for each page
        (each with a new random fingerprint). When False (default),
        one browser is reused for the whole pagination run.
        """
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        retry_async = _import_retry_async()
        retryable_errors = _import_retryable_errors()

        current_page_num = start_page
        processed_pages = 0
        seen_uuids: set[str] = set()
        idle_pages = 0

        if self.randomize_fingerprint:
            # Per-page browser: each page gets a fresh
            # InvisiblePlaywright instance with a new random
            # fingerprint (seed=None → secrets.randbits(31)).
            async for page_num, payload in self._iter_pages_randomized(
                product_path=product_path,
                start_page=start_page,
                max_pages=max_pages,
                retry_attempts=retry_attempts,
                seen_uuids=seen_uuids,
                idle_pages_ref=[idle_pages],
                processed_pages_ref=[processed_pages],
            ):
                yield page_num, payload
            return

        # When a proxy_pool is provided, we MUST use per-page browser
        # creation (like randomize_fingerprint) so each page can use
        # a different proxy. Override randomize_fingerprint if needed.
        if self.proxy_pool is not None and not self.randomize_fingerprint:
            print(
                "Ozon (public): proxy_pool активен — переключаю в "
                "режим per-page browser для ротации proxy"
            )
            async for page_num, payload in self._iter_pages_randomized(
                product_path=product_path,
                start_page=start_page,
                max_pages=max_pages,
                retry_attempts=retry_attempts,
                seen_uuids=seen_uuids,
                idle_pages_ref=[idle_pages],
                processed_pages_ref=[processed_pages],
            ):
                yield page_num, payload
            return

        # Single browser for the whole run (default, faster).
        proxy_for_run = self._get_proxy_for_page()
        sleep_with_jitter = _import_sleep_with_jitter()
        async with _import_invisible_playwright()(
            proxy=proxy_for_run,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await self._new_page_with_stealth(browser)

            # Warm up once per browser session: land on the product
            # page so Ozon sets its antibot cookies before we start
            # paginating through the reviews.
            await self._warmup_goto(
                page,
                product_path=product_path,
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                label="Ozon public",
            )

            prev_reviews_url: str | None = None
            product_url = f"https://www.ozon.ru{product_path}"

            while True:
                if (
                    max_pages is not None
                    and processed_pages >= max_pages
                ):
                    return

                if idle_pages >= self.max_idle_pages:
                    print(
                        f"Ozon (public): {idle_pages} подряд "
                        "пустых страниц; сбор завершён"
                    )
                    return

                reviews_url = (
                    f"https://www.ozon.ru"
                    f"{product_path}/reviews?page={current_page_num}"
                )

                # Navigate like a real visitor: page 1 comes from
                # the product card, page N+1 from page N.
                await self._goto_reviews_like_human(
                    page,
                    product_path=product_path,
                    reviews_url=reviews_url,
                    referer_url=prev_reviews_url or product_url,
                    retry_async=retry_async,
                    retryable_errors=retryable_errors,
                    label=f"Ozon public page {current_page_num}",
                    goto_attempts=retry_attempts,
                    do_warmup=False,
                )
                prev_reviews_url = reviews_url

                # A page fetch can land on an antibot challenge
                # even after a warmed-up navigation (measured
                # 2026-09: the outcome is random per request). When
                # that happens, cool down and re-fetch the SAME page
                # in this (cookie-warm) browser instead of skipping
                # to the next page number.
                cards: list[dict[str, Any]] | None = None
                antibot_attempts = max(1, retry_attempts)
                for attempt in range(1, antibot_attempts + 1):
                    if self.settle_ms > 0:
                        await page.wait_for_timeout(self.settle_ms)

                    review_locator = page.locator(
                        "[data-review-uuid]"
                    )
                    try:
                        await review_locator.first.wait_for(
                            state="attached",
                            timeout=self.card_wait_ms,
                        )
                        cards = await self._read_review_cards(
                            review_locator,
                        )
                        break
                    except Exception:
                        antibot = await self._page_is_antibot(page)
                        if not antibot:
                            break
                        await self._save_debug_page(
                            page=page,
                            page_number=current_page_num,
                        )
                        if attempt >= antibot_attempts:
                            print(
                                f"Ozon (public): page "
                                f"{current_page_num} — antibot/"
                                "challenge на всех попытках"
                            )
                            break
                        cooldown = min(5.0 * attempt, 30.0)
                        print(
                            f"Ozon (public): page "
                            f"{current_page_num} — antibot/"
                            f"challenge, пауза {cooldown:.0f}s и "
                            f"повтор ({attempt}/{antibot_attempts})"
                        )
                        await sleep_with_jitter(cooldown)
                        await self._goto_reviews_like_human(
                            page,
                            product_path=product_path,
                            reviews_url=reviews_url,
                            referer_url=prev_reviews_url,
                            retry_async=retry_async,
                            retryable_errors=retryable_errors,
                            label=(
                                f"Ozon public page {current_page_num}"
                            ),
                            goto_attempts=retry_attempts,
                            do_warmup=True,
                        )

                if cards is None:
                    # No review cards on this page — either the
                    # page has no more reviews or Cloudflare served
                    # a challenge/interstitial. Treat as idle.
                    print(
                        f"Ozon (public): page {current_page_num} — "
                        "no [data-review-uuid] cards found"
                    )
                    await self._save_debug_page(
                        page=page,
                        page_number=current_page_num,
                    )
                    idle_pages += 1
                    current_page_num += 1
                    continue

                # Filter out already-seen UUIDs.
                new_cards = []
                for card in cards:
                    uuid = card.get("uuid")
                    if uuid and uuid in seen_uuids:
                        continue
                    if uuid:
                        seen_uuids.add(uuid)
                    new_cards.append(card)

                await self._save_debug_page(
                    page=page,
                    page_number=current_page_num,
                    cards=new_cards,
                )

                if not new_cards:
                    idle_pages += 1
                    print(
                        f"Ozon (public): page {current_page_num} — "
                        f"0 новых отзывов (idle={idle_pages})"
                    )
                else:
                    idle_pages = 0
                    print(
                        f"Ozon (public): page {current_page_num} — "
                        f"{len(new_cards)} новых отзывов"
                    )

                processed_pages += 1

                # Build a payload shaped like the Ozon API response
                # so the existing adapter code can consume it
                # unchanged. The adapter's
                # ``extract_reviews_from_ozon_payload`` walks the
                # payload recursively looking for review-ish dicts;
                # our card dicts have ``reviewId`` set to the UUID
                # so they match the heuristic.
                payload = {
                    "reviews": [
                        {
                            "reviewId": card.get("uuid"),
                            "rating": card.get("rating"),
                            "text": card.get("text"),
                            "author": card.get("author"),
                            "published_at": card.get("published_at"),
                            # nextPage hint: we always say "yes,
                            # there's a next page" because we
                            # increment the counter ourselves. The
                            # adapter only uses this to decide
                            # whether to continue iteration.
                            "nextPage": (
                                f"{product_path}/reviews?"
                                f"page={current_page_num + 1}"
                            ),
                        }
                        for card in new_cards
                    ],
                    # nextPage is a hint to the adapter; we drive
                    # iteration ourselves via current_page_num, so
                    # we always provide it (the adapter falls back
                    # to building review_id from page+position when
                    # reviewId is missing).
                    "nextPage": (
                        f"{product_path}/reviews?"
                        f"page={current_page_num + 1}"
                    ),
                }

                yield current_page_num, payload

                current_page_num += 1

    async def _iter_pages_randomized(
        self,
        *,
        product_path: str,
        start_page: int,
        max_pages: int | None,
        retry_attempts: int,
        seen_uuids: set[str],
        idle_pages_ref: list[int],
        processed_pages_ref: list[int],
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Per-page browser iteration with random fingerprint.

        Opens a fresh ``InvisiblePlaywright`` instance for each
        page, so each page gets a new random fingerprint (when
        ``seed=None``). Slower (~2-5s browser startup per page) but
        maximally stealthy — every page looks like a different
        browser to Cloudflare.
        """
        retry_async = _import_retry_async()
        retryable_errors = _import_retryable_errors()

        current_page_num = start_page

        while True:
            if (
                max_pages is not None
                and processed_pages_ref[0] >= max_pages
            ):
                return

            if idle_pages_ref[0] >= self.max_idle_pages:
                print(
                    f"Ozon (public-rand): {idle_pages_ref[0]} подряд "
                    "пустых страниц; сбор завершён"
                )
                return

            reviews_url = (
                f"https://www.ozon.ru"
                f"{product_path}/reviews?page={current_page_num}"
            )

            # One page may take several attempts: Ozon randomly
            # serves an antibot/challenge page or its «Похоже, нет
            # соединения» error page even for identical requests
            # (measured 2026-09). Each retry rotates to the next
            # proxy with a fresh random fingerprint and a cooldown.
            sleep_with_jitter = _import_sleep_with_jitter()
            cards: list[dict[str, Any]] | None = None
            attempts_left = max(1, retry_attempts)
            attempt_num = 0
            while attempts_left > 0:
                attempts_left -= 1
                attempt_num += 1
                cards, antibot = await self._fetch_page_cards(
                    reviews_url=reviews_url,
                    product_path=product_path,
                    page_number=current_page_num,
                    retry_attempts=retry_attempts,
                )
                if cards is not None:
                    break
                if antibot and attempts_left > 0:
                    cooldown = min(5.0 * attempt_num, 30.0)
                    print(
                        f"Ozon (public-rand): page "
                        f"{current_page_num} — antibot/challenge; "
                        f"пауза {cooldown:.0f}s, ротирую proxy и "
                        "повторяю страницу"
                    )
                    await sleep_with_jitter(cooldown)
                    continue
                break

            if cards is None:
                print(
                    f"Ozon (public-rand): page "
                    f"{current_page_num} — no "
                    "[data-review-uuid] cards found"
                )
                idle_pages_ref[0] += 1
                current_page_num += 1
                continue

            new_cards = []
            for card in cards:
                uuid = card.get("uuid")
                if uuid and uuid in seen_uuids:
                    continue
                if uuid:
                    seen_uuids.add(uuid)
                new_cards.append(card)

            if not new_cards:
                idle_pages_ref[0] += 1
                print(
                    f"Ozon (public-rand): page "
                    f"{current_page_num} — 0 новых отзывов "
                    f"(idle={idle_pages_ref[0]})"
                )
            else:
                idle_pages_ref[0] = 0
                print(
                    f"Ozon (public-rand): page "
                    f"{current_page_num} — "
                    f"{len(new_cards)} новых отзывов"
                )

            processed_pages_ref[0] += 1

            payload = {
                "reviews": [
                    {
                        "reviewId": card.get("uuid"),
                        "rating": card.get("rating"),
                        "text": card.get("text"),
                        "author": card.get("author"),
                        "published_at": card.get("published_at"),
                        "nextPage": (
                            f"{product_path}/reviews?"
                            f"page={current_page_num + 1}"
                        ),
                    }
                    for card in new_cards
                ],
                "nextPage": (
                    f"{product_path}/reviews?"
                    f"page={current_page_num + 1}"
                ),
            }

            yield current_page_num, payload

            current_page_num += 1

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
        scroll_proxy = self._get_proxy_for_page()
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
                f"https://www.ozon.ru{product_path}/reviews?page=1"
            )

            # Same human-like navigation as pagination: warm up on
            # the product page, then open the reviews with referer.
            await self._goto_reviews_like_human(
                page,
                product_path=product_path,
                reviews_url=reviews_url,
                referer_url=(
                    f"https://www.ozon.ru{product_path}"
                ),
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

            for round_num in range(1, 1000):
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
        """Run pagination first, then scroll as a supplement.

        Yields ``(strategy, node)`` tuples. ``strategy`` is
        ``"pagination"`` or ``"scroll"``.
        """
        from shared.retry import sleep_with_jitter

        seen_ids: set[str] = set()

        # --- pagination ---
        # The browser launch includes an egress-IP check through the
        # proxy; paid rotating pools (e.g. proxys.io) intermittently
        # answer 503 to CONNECT. Retry the phase — the pool rotates to
        # another exit IP on each attempt.
        pagination_attempts = max(3, retry_attempts)
        for attempt in range(1, pagination_attempts + 1):
            try:
                async for page_num, payload in self.iter_ozon_reviews_json(
                    product_path=product_path,
                    start_page=pagination_start_page,
                    max_pages=pagination_max_pages,
                    retry_attempts=retry_attempts,
                ):
                    for node in payload.get("reviews", []):
                        rid = node.get("reviewId") or node.get("uuid")
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

                    if page_delay_seconds > 0:
                        await sleep_with_jitter(page_delay_seconds)
                break
            except Exception as exc:
                if attempt >= pagination_attempts:
                    print(
                        "Ozon (public auto): pagination phase failed "
                        f"after {attempt} attempts: {exc}"
                    )
                else:
                    print(
                        "Ozon (public auto): pagination attempt "
                        f"{attempt}/{pagination_attempts} failed: "
                        f"{exc} — retry with next proxy"
                    )
                    await sleep_with_jitter(5.0)

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
        async for current_page, payload in self.iter_ozon_reviews_json(
            product_path=product_path,
            start_page=page_number,
            max_pages=1,
        ):
            return payload
        raise RuntimeError(
            f"Ozon (public): не удалось получить страницу {page_number}"
        )

    # ------------------------------------------------------------------
    # DOM card reader
    # ------------------------------------------------------------------
    async def _read_review_cards(self, review_locator) -> list[dict[str, Any]]:
        """Read all ``[data-review-uuid]`` cards on the current page.

        Each card becomes a dict with:
          - ``uuid``: review UUID (from ``data-review-uuid``)
          - ``published_at``: Unix timestamp (from ``publishedat``)
          - ``status_id``: moderation status (from ``statusid``)
          - ``text``: raw inner_text of the card (avatar initials,
            author, date, review text, "Вам помог этот отзыв?",
            "Да N Нет M")
          - ``rating``: 1-5 (from SVG star colors) or None
          - ``images``: list of img src URLs

        The adapter's ``parse_ozon_dom_card`` will further extract
        ``author``, ``date``, ``review_text`` from the lines.
        """
        result: list[dict[str, Any]] = []
        count = await review_locator.count()

        for index in range(count):
            card = review_locator.nth(index)

            uuid = await card.get_attribute("data-review-uuid")
            published_at = await card.get_attribute("publishedat")
            status_id = await card.get_attribute("statusid")
            text = await card.inner_text()
            rating = await self._read_review_rating(card)
            images = await self._read_images(card)

            result.append({
                "uuid": uuid,
                "published_at": published_at,
                "status_id": status_id,
                "text": text,
                "rating": rating,
                "images": images,
            })

        return result

    async def _read_review_rating(self, card) -> int | None:
        """Read the per-review star rating by counting filled SVG
        stars. Mirrors ``BrowserDomTransport._read_review_rating``.
        """
        rating_container = card.locator('[class*="rpProducta9c"]')

        if await rating_container.count() == 0:
            return None

        stars = rating_container.locator("svg")
        star_count = await stars.count()

        if star_count == 0:
            return None

        filled = 0
        for star_index in range(star_count):
            star = stars.nth(star_index)
            try:
                color = await star.evaluate(
                    """
                    (element) => {
                        const path = element.querySelector("path");
                        if (!path) {
                            return null;
                        }
                        return {
                            elementColor:
                                getComputedStyle(element).color,
                            pathFill:
                                getComputedStyle(path).fill,
                            pathAttribute:
                                path.getAttribute("fill"),
                            className:
                                element.getAttribute("class") || ""
                        };
                    }
                    """
                )
            except Exception:
                continue
            if self._is_filled_star(color):
                filled += 1

        return filled if filled else None

    @staticmethod
    def _is_filled_star(color: dict[str, Any] | None) -> bool:
        """Heuristic for deciding whether a star SVG is filled."""
        if not color:
            return False
        values = " ".join(
            str(value).lower()
            for value in color.values()
            if value is not None
        )
        yellow_markers = (
            "rgb(255, 168",
            "rgb(255, 170",
            "rgb(255, 184",
            "rgb(255, 185",
            "#f",
            "currentcolor",
        )
        return any(marker in values for marker in yellow_markers)

    async def _read_images(self, card) -> list[str]:
        """Collect img src URLs from the card."""
        result: list[str] = []
        images = card.locator("img")
        for index in range(await images.count()):
            src = await images.nth(index).get_attribute("src")
            if src:
                result.append(src)
        return result

    # ------------------------------------------------------------------
    # Page lifecycle / stealth
    # ------------------------------------------------------------------
    async def _new_page_with_stealth(self, browser):
        """Create a new page.

        NOTE: no JS stealth init script is applied here, and that
        is deliberate. Measured 2026-09-15 on this exact product
        page: WITH the playwright-stealth-style script Ozon serves
        its «Похоже, нет соединения» error page (0 cards); WITHOUT
        it the same proxy/fingerprint gets HTTP 200 and 30 review
        cards — on both the invisible-playwright (Firefox) and the
        plain Chromium engines. invisible-playwright already
        provides the real stealth (patched engine, fingerprint,
        humanized input); layering the JS patch on top only breaks
        the page's own JS (fake ``navigator.plugins`` lacks the
        PluginArray methods Ozon's code expects, and a fake
        ``window.chrome`` contradicts a Firefox engine).

        The ``stealth`` constructor flag is kept for CLI
        compatibility but no longer injects anything."""
        return await browser.new_page()

    async def _fetch_page_cards(
        self,
        *,
        reviews_url: str,
        product_path: str,
        page_number: int,
        retry_attempts: int,
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        """Load one reviews page in a fresh randomized browser.

        Returns ``(cards, antibot)``:

        - ``cards`` — parsed card dicts (possibly empty when the
          page genuinely has no reviews), or ``None`` when the
          review cards never appeared.
        - ``antibot`` — True when the no-cards state looks like an
          antibot/challenge page; the caller should cool down,
          rotate the proxy and retry the page instead of treating
          it as empty.
        """
        retry_async = _import_retry_async()
        retryable_errors = _import_retryable_errors()

        # Fresh browser per fetch: new random fingerprint (seed
        # defaults to None → secrets.randbits(31)) and, when a
        # proxy_pool is active, the next proxy in the rotation.
        page_proxy = self._get_proxy_for_page()
        if page_proxy is not None and self.proxy_pool is not None:
            print(
                f"Ozon (public-rand): page {page_number} — "
                f"proxy: {page_proxy.get('server', 'unknown')}"
            )

        # The whole browser session is one attempt: a rotating
        # proxy gateway can fail it at ANY point — refused CONNECT
        # at launch ("could not discover the egress IP"), or the
        # exit IP drifting mid-session (invisible-playwright's
        # ProxyEgressDrifted, measured on proxys.io: the gateway
        # re-rolls the exit every few minutes). None of these say
        # anything about the PAGE — so any session-level failure is
        # converted to "rotate the proxy and retry the page".
        try:
            return await self._fetch_page_cards_in_session(
                reviews_url=reviews_url,
                product_path=product_path,
                page_number=page_number,
                page_proxy=page_proxy,
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                retry_attempts=retry_attempts,
            )
        except Exception as exc:
            print(
                f"Ozon (public-rand): page {page_number} — сессия "
                f"не удалась ({type(exc).__name__}: "
                f"{str(exc)[:120]}) — ротирую proxy"
            )
            self._mark_proxy_blocked(page_proxy)
            return None, True

    async def _fetch_page_cards_in_session(
        self,
        *,
        reviews_url: str,
        product_path: str,
        page_number: int,
        page_proxy: dict[str, str] | None,
        retry_async,
        retryable_errors,
        retry_attempts: int,
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        """One page fetch inside a single fresh browser session.

        Returns ``(cards, antibot)`` — see ``_fetch_page_cards``.
        Raises on session-level failures (launch, egress drift);
        the caller converts those into a proxy rotation."""
        async with _import_invisible_playwright()(
            proxy=page_proxy,
            seed=None,  # randomize on every fetch
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await self._new_page_with_stealth(browser)

            await self._goto_reviews_like_human(
                page,
                product_path=product_path,
                reviews_url=reviews_url,
                referer_url=(
                    f"https://www.ozon.ru{product_path}"
                ),
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                label=f"Ozon public-rand page {page_number}",
                goto_attempts=retry_attempts,
                do_warmup=True,
            )

            if self.settle_ms > 0:
                await page.wait_for_timeout(self.settle_ms)

            review_locator = page.locator("[data-review-uuid]")
            try:
                await review_locator.first.wait_for(
                    state="attached",
                    timeout=self.card_wait_ms,
                )
            except Exception:
                antibot = await self._page_is_antibot(page)
                await self._save_debug_page(
                    page=page,
                    page_number=page_number,
                )
                if antibot:
                    # The proxy's exit IP just got flagged — take it
                    # out of the rotation so the next attempt uses a
                    # different IP.
                    self._mark_proxy_blocked(page_proxy)
                return None, antibot

            cards = await self._read_review_cards(review_locator)
            await self._save_debug_page(
                page=page,
                page_number=page_number,
                cards=cards,
            )
            return cards, False

    def _get_proxy_for_page(self) -> dict[str, str] | None:
        """Return the proxy to use for the next page fetch.

        When a ``proxy_pool`` is configured, returns the next proxy
        in the rotation. When ``proxy`` is configured (single
        proxy), returns that. When neither is configured, returns
        ``None`` (direct connection).
        """
        if self.proxy_pool is not None:
            proxy = self.proxy_pool.next()
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
        """Save a screenshot + the parsed cards for postmortem."""
        page_dir = self.debug_dir / f"page_{page_number}"
        page_dir.mkdir(parents=True, exist_ok=True)

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
