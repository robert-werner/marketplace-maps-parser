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
        seed: int | None = None,
        pin: dict[str, Any] | None = None,
        humanize: bool = True,
        stealth: bool = True,
        max_idle_pages: int = 2,
        scroll_max_idle_rounds: int = 5,
        scroll_step: int = 1800,
        scroll_pause_ms: int = 800,
        randomize_fingerprint: bool = False,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
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

        # Single browser for the whole run (default, faster).
        async with _import_invisible_playwright()(
            proxy=self.proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await self._new_page_with_stealth(browser)

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

                # Navigate to the public review page. Cloudflare
                # almost never challenges plain HTML navigations.
                await retry_async(
                    lambda url=reviews_url: page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    ),
                    attempts=retry_attempts,
                    base_delay=3.0,
                    max_delay=30.0,
                    factor=2.0,
                    jitter=0.3,
                    retry_on=retryable_errors(),
                    label=f"Ozon public page {current_page_num}",
                )

                if self.settle_ms > 0:
                    await page.wait_for_timeout(self.settle_ms)

                # Wait for review cards to attach.
                review_locator = page.locator("[data-review-uuid]")
                try:
                    await review_locator.first.wait_for(
                        state="attached",
                        timeout=30_000,
                    )
                except Exception:
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

                # Read all visible cards.
                cards = await self._read_review_cards(review_locator)

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

            # Open a fresh browser for this page. seed=None →
            # secrets.randbits(31) → new random fingerprint every
            # time.
            async with _import_invisible_playwright()(
                proxy=self.proxy,
                seed=None,  # randomize on every page
                pin=self.pin,
                humanize=self.humanize,
            ) as browser:
                page = await self._new_page_with_stealth(browser)

                await retry_async(
                    lambda url=reviews_url: page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    ),
                    attempts=retry_attempts,
                    base_delay=3.0,
                    max_delay=30.0,
                    factor=2.0,
                    jitter=0.3,
                    retry_on=retryable_errors(),
                    label=f"Ozon public-rand page {current_page_num}",
                )

                if self.settle_ms > 0:
                    await page.wait_for_timeout(self.settle_ms)

                review_locator = page.locator("[data-review-uuid]")
                try:
                    await review_locator.first.wait_for(
                        state="attached",
                        timeout=30_000,
                    )
                except Exception:
                    print(
                        f"Ozon (public-rand): page "
                        f"{current_page_num} — no "
                        "[data-review-uuid] cards found"
                    )
                    await self._save_debug_page(
                        page=page,
                        page_number=current_page_num,
                    )
                    idle_pages_ref[0] += 1
                    current_page_num += 1
                    continue

                cards = await self._read_review_cards(review_locator)

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
        async with _import_invisible_playwright()(
            proxy=self.proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await self._new_page_with_stealth(browser)

            reviews_url = (
                f"https://www.ozon.ru{product_path}/reviews?page=1"
            )

            await page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            if self.settle_ms > 0:
                await page.wait_for_timeout(self.settle_ms)

            review_locator = page.locator("[data-review-uuid]")
            try:
                await review_locator.first.wait_for(
                    state="attached",
                    timeout=30_000,
                )
            except Exception as exc:
                raise RuntimeError(
                    "Ozon (public scroll): no [data-review-uuid] "
                    f"cards found on {reviews_url}: {exc}"
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
        except Exception as exc:
            print(
                "Ozon (public auto): pagination phase failed: "
                f"{exc} — пробую scroll"
            )

        if max_reviews is not None and len(seen_ids) >= max_reviews:
            return

        # --- scroll supplement ---
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

                    # Wrap the DOM card in a shape that the adapter's
                    # map_ozon_review_node / parse_ozon_dom_card can
                    # consume.
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
        except Exception as exc:
            print(
                "Ozon (public auto): scroll phase failed: "
                f"{exc}"
            )

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
        """Create a new page and apply stealth init script if
        enabled."""
        page = await browser.new_page()
        if self.stealth:
            try:
                from infrastructure.transports.browser_json import (
                    _STEALTH_INIT_SCRIPT,
                )
                await page.add_init_script(_STEALTH_INIT_SCRIPT)
            except Exception as exc:
                print(
                    "Ozon (public): warning — не удалось применить "
                    f"stealth init script: {exc}"
                )
        return page

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
