"""Mixin for PublicPageTransport."""
from __future__ import annotations
from collections.abc import AsyncIterator
from typing import Any
from infrastructure.transports.base import OZON_BASE_URL
import infrastructure.transports.public_page as _mod


class PaginationMixin:
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
        extra_query: str = "",
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

        ``extra_query`` appends stream-variant parameters (e.g.
        ``&sort=score_asc``) to every page URL.
        """
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        retry_async = _mod._import_retry_async()
        retryable_errors = _mod._import_retryable_errors()

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
                extra_query=extra_query,
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
                extra_query=extra_query,
            ):
                yield page_num, payload
            return

        # Single browser for the whole run (default, faster).
        proxy_for_run = await self._get_proxy_for_page()
        sleep_with_jitter = _mod._import_sleep_with_jitter()
        async with _mod._import_invisible_playwright()(
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
            product_url = self._absolute_url(product_path)

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
                    f"{OZON_BASE_URL}"
                    f"{product_path}/reviews"
                    f"?page={current_page_num}{extra_query}"
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
                        self._notify_pacer_block()
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
        extra_query: str = "",
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Per-page browser iteration with random fingerprint.

        Opens a fresh ``InvisiblePlaywright`` instance for each
        page, so each page gets a new random fingerprint (when
        ``seed=None``). Slower (~2-5s browser startup per page) but
        maximally stealthy — every page looks like a different
        browser to Cloudflare.
        """
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
                f"{OZON_BASE_URL}"
                f"{product_path}/reviews"
                f"?page={current_page_num}{extra_query}"
            )

            # One page may take several attempts: Ozon randomly
            # serves an antibot/challenge page or its «Похоже, нет
            # соединения» error page even for identical requests
            # (measured 2026-09). Each retry rotates to the next
            # proxy with a fresh random fingerprint and a cooldown.
            sleep_with_jitter = _mod._import_sleep_with_jitter()
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
                    self._notify_pacer_block()
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
