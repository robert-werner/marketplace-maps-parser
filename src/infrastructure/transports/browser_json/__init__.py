from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

from infrastructure.transports.base import OZON_BASE_URL

# Retry semantics are shared with the other
# browser transports — see browser_common.py.
from infrastructure.transports.browser_common import (
    _get_retryable_errors as _get_retryable_errors,
)
from infrastructure.transports.browser_common import (
    _retryable_errors as _retryable_errors,
)
from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
from infrastructure.transports.browser_json._dom import CardReadingMixin
from infrastructure.transports.browser_json._errors import (
    CloudflareChallengeError as CloudflareChallengeError,
)
from infrastructure.transports.browser_json._fetch import FetchMixin
from shared.async_iterators import closing_iterator
from shared.pacing import AdaptivePacer


def _import_invisible_playwright() -> type:
    """Back-compat shim: the lazy factory moved to
    browser_common.import_invisible_playwright (tests patch the
    module attribute by this name)."""
    return import_invisible_playwright()


class BrowserJsonTransport(FetchMixin, CardReadingMixin):
    """Получает JSON Ozon в одной browser-сессии.

    Сначала открывается страница отзывов, затем внутренний endpoint
    вызывается из этой же страницы через window.fetch(). Следующая
    страница берётся из поля nextPage ответа Ozon.

    Three fetch strategies are supported (controlled by
    ``fetch_strategy`` constructor arg):

    - ``"auto"`` (default): fetch in a ready reviews document, falling
      back to API navigation once per tab.
    - ``"navigation"``: ``page.goto(api_url)`` with raw JSON extraction.
    - ``"fetch"``: only ``page.evaluate(fetch(api_url))``.

    The ``stealth`` argument is retained for compatibility. Invisible
    Playwright owns the fingerprint inside its patched browser engine;
    this transport deliberately does not add a page-level JS shim.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 2_000,
        debug_dir: str = "debug_ozon",
        proxy: dict[str, str] | None = None,
        proxy_pool: Any | None = None,
        seed: int | None = None,
        pin: dict[str, Any] | None = None,
        humanize: bool = True,
        fetch_strategy: str = "auto",
        stealth: bool = True,
        # Playwright-format cookies of a logged-in Ozon session
        # (see cookie_loader). Injected into every page before the
        # first navigation — the internal API needs them to serve
        # the full review list instead of the anonymous subset.
        cookies: list[dict[str, Any]] | None = None,
        # Save a page screenshot into the debug dir on every debug
        # dump. OFF by default: full-page screenshots of a logged-in
        # session are a PII hazard and cost a noticeable share of the
        # per-page wall time. Raw dumps are also opt-in.
        screenshots: bool = False,
        # Abort image/font/media requests on scraper navigations.
        # The JSON fetch needs none of these assets. Route by file
        # extension rather than sending every request through Python.
        block_assets: bool = True,
        debug_dumps: bool = False,
        page_delay_seconds: float = 0.8,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.proxy_pool = proxy_pool
        self.seed = seed
        self.pin = pin
        self.humanize = humanize
        self.stealth = stealth
        self.cookies = cookies
        self.screenshots = screenshots
        self.block_assets = block_assets
        self.debug_dumps = debug_dumps or screenshots
        self.page_delay_seconds = page_delay_seconds
        self._page_fetch_modes: WeakKeyDictionary[Any, str] = (
            WeakKeyDictionary()
        )
        self._page_pacers: WeakKeyDictionary[Any, AdaptivePacer] = (
            WeakKeyDictionary()
        )
        self.last_review_count: int | None = None
        self.last_product_title: str | None = None
        self.incomplete_reason: str | None = None
        if fetch_strategy not in ("auto", "navigation", "fetch"):
            raise ValueError(
                f"Unknown fetch_strategy: {fetch_strategy!r}. "
                "Use 'auto', 'navigation' or 'fetch'."
            )
        self.fetch_strategy = fetch_strategy

    async def _next_session_proxy(self) -> dict[str, str] | None:
        if self.proxy_pool is None:
            return self.proxy
        next_async = getattr(self.proxy_pool, "next_async", None)
        return cast(
            dict[str, str] | None,
            await next_async()
            if next_async is not None
            else self.proxy_pool.next(),
        )

    @asynccontextmanager
    async def _browser_context(self) -> AsyncIterator[Any]:
        """Create an Invisible Playwright session for pagination or scroll."""
        proxy = await self._next_session_proxy()
        async with _import_invisible_playwright()(
            proxy=proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            yield browser

    async def _inject_cookies(self, page: Any) -> None:
        """Logged-in session cookies into the page's context BEFORE
        the first navigation (no-op without cookies; a failure is a
        warning, not fatal)."""
        if not self.cookies:
            return
        try:
            await page.context.add_cookies(self.cookies)
        except Exception as exc:
            print(
                "Ozon (playwright): WARNING — не удалось подставить "
                f"cookies ({type(exc).__name__}: {exc})"
            )

    async def _settle_after_goto(self, page: Any) -> None:
        """Do not start fetch on a challenge document or an old context."""
        if self.fetch_strategy in ("auto", "fetch"):
            await self._wait_for_reviews_ready(page)
            return
        # The API navigation already returns the response body. Keep only
        # a small beacon grace period; the old 500 ms was paid per page.
        await page.wait_for_timeout(150)

    async def iter_ozon_reviews_json(
            self,
            product_path: str,
            *,
            start_page: int = 1,
            max_pages: int | None = None,
            retry_attempts: int = 3,
            extra_query: str = "",
            page_delay_seconds: float | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Paginate the internal Ozon reviews API.

        ``extra_query`` appends stream-variant parameters to the
        FIRST page URL (e.g. ``&sort=score_asc``); subsequent pages
        follow Ozon's ``nextPage`` verbatim, so the variant applies
        to the whole stream when Ozon echoes it in nextPage.
        """
        pacer = AdaptivePacer(
            base_delay=(
                self.page_delay_seconds
                if page_delay_seconds is None else page_delay_seconds
            ),
        )

        async with self._browser_context() as browser:

            # Page factory: returns a fresh page for retries and
            # recovery paths; the happy path instead REUSES one tab
            # (see the ``page=`` argument of _goto_with_retry) so a
            # stream looks like a user walking pages in one tab —
            # and skips the per-page new_page/init-script cost.
            #
            # We close the previous page before creating a new one so
            # we don't leak browser tabs. The first call has nothing
            # to close; subsequent calls close the page that was
            # returned by the previous successful _goto_with_retry /
            # _fetch_json_with_retry cycle.
            current_page_holder: dict[str, Any] = {"page": None}

            async def page_factory() -> Any:
                old = current_page_holder["page"]
                if old is not None:
                    try:
                        await old.close()
                    except Exception:
                        pass
                new_page = await browser.new_page()
                self._page_pacers[new_page] = pacer
                await self._inject_cookies(new_page)
                await self._install_resource_blocker(new_page)
                current_page_holder["page"] = new_page
                return new_page

            page = await page_factory()
            page_ready_for_api = False

            current_path = self._build_initial_path(
                product_path=product_path,
                page_number=start_page,
            ) + extra_query

            seen_paths: set[str] = set()
            processed_pages = 0
            # Debug dumps are written per page NUMBER; with several
            # streams (default / score_asc / score_desc) walking in
            # parallel — or even sequentially — page_1 of one stream
            # would overwrite page_1 of another. Derive a per-stream
            # suffix from extra_query so each stream dumps into its
            # own page_N_<sort> directory.
            stream_suffix = ""
            if extra_query and "sort=" in extra_query:
                stream_suffix = "_" + extra_query.split("sort=")[
                    -1
                ].strip("&")
            # Track the page_key of the previous successful request so
            # we can detect a page_key transition (see
            # ``_reset_page_in_path`` docstring for the rationale).
            prev_page_key: str | None = None
            # True if we have already attempted the page=1 fallback for
            # the current page_key transition. Prevents infinite loops
            # if the fallback itself returns 0 reviews.
            page_key_reset_done = False

            while current_path:
                if (
                        max_pages is not None
                        and processed_pages >= max_pages
                ):
                    return

                if current_path in seen_paths:
                    print(
                        "Ozon: повторный nextPage, остановка: "
                        f"{current_path}"
                    )
                    return

                seen_paths.add(current_path)

                if processed_pages:
                    await pacer.wait()
                reviews_url = self._absolute_url(current_path)

                # Warm the reviews page only once per browser session.
                # Subsequent pages are internal API navigations/fetches on
                # the same tab; revisiting the HTML reviews page before every
                # API request was the largest avoidable latency multiplier.
                if not page_ready_for_api:
                    page = await self._goto_with_retry(
                        page_factory=page_factory,
                        page=page,
                        reviews_url=reviews_url,
                        attempts=retry_attempts,
                        label=(
                            f"Ozon warmup page "
                            f"{processed_pages + 1} ({current_path})"
                        ),
                    )
                    await self._settle_after_goto(page)
                    page_ready_for_api = True

                # If the fetch fails with an execution-context-lost
                # style error, recreate the page and retry. This is a
                # second line of defense after _goto_with_retry: the
                # goto can succeed but the page can still die before
                # the fetch runs.
                try:
                    payload = await self._fetch_json_with_retry(
                        page=page,
                        internal_path=current_path,
                        attempts=retry_attempts,
                        label=(
                            f"Ozon pagination page "
                            f"{processed_pages + 1} "
                            f"({current_path})"
                        ),
                    )
                except _retryable_errors() as exc:
                    print(
                        f"Ozon: fetch retry exhausted on page "
                        f"{processed_pages + 1}; пересоздаю страницу "
                        f"и повторяю один раз: {exc}"
                    )
                    try:
                        page = await page_factory()
                        await page.goto(
                            reviews_url,
                            wait_until="domcontentloaded",
                            timeout=self.timeout_ms,
                        )
                        await self._settle_after_goto(page)
                        page_ready_for_api = True
                        payload = await self._fetch_json_with_retry(
                            page=page,
                            internal_path=current_path,
                            attempts=retry_attempts,
                            label=(
                                f"Ozon pagination page "
                                f"{processed_pages + 1} "
                                f"({current_path}) [retry-after-recreate]"
                            ),
                        )
                    except _retryable_errors() as exc2:
                        raise RuntimeError(
                            f"Ozon: page {processed_pages + 1} "
                            "failed after browser recreation"
                        ) from exc2

                self._validate_reviews_payload(payload)
                processed_pages += 1
                pacer.record_success()

                await self._save_debug(
                    page=page,
                    payload=payload,
                    page_number=processed_pages,
                    stream_suffix=stream_suffix,
                )

                from infrastructure.marketplaces.ozon_payload import (
                    extract_ozon_product_title,
                    extract_ozon_rating_summary,
                )

                summary = extract_ozon_rating_summary(payload)
                total = (summary or {}).get("reviews_count")
                if isinstance(total, int) and not isinstance(total, bool):
                    self.last_review_count = max(
                        total, self.last_review_count or 0,
                    )
                if not self.last_product_title:
                    self.last_product_title = extract_ozon_product_title(
                        payload,
                    )

                # ------------------------------------------------------
                # page_key transition detection
                # ------------------------------------------------------
                current_page_key = self._extract_query_param(
                    current_path, "page_key",
                )
                page_key_changed = (
                    prev_page_key is not None
                    and current_page_key is not None
                    and current_page_key != prev_page_key
                )
                review_count = len(
                    self._extract_review_nodes_from_payload(payload),
                )

                if (
                    page_key_changed
                    and review_count == 0
                    and not page_key_reset_done
                ):
                    # Ozon handed us a nextPage with a new page_key but
                    # kept the old page=7 counter. The new variant
                    # starts at page=1 — retry with that.
                    assert prev_page_key is not None
                    assert current_page_key is not None
                    print(
                        "Ozon: смена page_key ("
                        f"{prev_page_key[:12]}... -> "
                        f"{current_page_key[:12]}...) с 0 отзывов; "
                        "повторяю запрос с page=1"
                    )
                    page_key_reset_done = True
                    reset_path = self._reset_page_in_path(
                        current_path, page=1,
                    )
                    if reset_path not in seen_paths:
                        # Don't yield this empty payload; retry the
                        # reset path on the next iteration.
                        current_path = reset_path
                        continue
                    # If the reset path was already seen, fall through
                    # to normal handling (yield + stop).

                prev_page_key = current_page_key
                # Reset the "fallback attempted" flag once we successfully
                # get past a transition (reviews > 0 OR we just did the
                # reset). This allows a subsequent transition later in
                # the stream to also be retried.
                if review_count > 0:
                    page_key_reset_done = False

                next_path = self.extract_next_path(payload)

                # ------------------------------------------------------
                # nextPage-loop guard
                # ------------------------------------------------------
                # When we just completed a page_key reset (page=1 of
                # variant B), Ozon's nextPage can point back to variant A
                # page 2 — a path we already saw. If we blindly followed
                # it we would either loop forever or stop short. Instead,
                # when the suggested next_path is already in seen_paths
                # AND we're currently on a non-empty variant (page_key is
                # set and we got reviews), we synthesize the next page
                # by incrementing the page counter on the CURRENT path
                # (which carries the right page_key).
                if (
                    next_path is not None
                    and next_path in seen_paths
                    and current_page_key is not None
                    and review_count > 0
                ):
                    # Try page+1 with the current page_key.
                    current_page_num = self._extract_query_param(
                        current_path, "page",
                    )
                    try:
                        next_num = int(current_page_num or "1") + 1
                    except (TypeError, ValueError):
                        next_num = 2
                    synthesized = self._reset_page_in_path(
                        current_path, page=next_num,
                    )
                    if synthesized not in seen_paths:
                        print(
                            "Ozon: nextPage уже был в seen_paths; "
                            f"синтезирую следующий URL для текущего "
                            f"page_key ({current_page_key[:12]}...): "
                            f"page={next_num}"
                        )
                        next_path = synthesized

                yield processed_pages, payload

                if not next_path:
                    print(
                        f"Ozon: у страницы {processed_pages} "
                        "нет nextPage; сбор завершён"
                    )
                    return

                current_path = next_path

    def _validate_reviews_payload(self, payload: dict[str, Any]) -> None:
        """A layout/error response must not become a successful empty crawl."""
        if any(name in payload for name in ("incidentId", "challengeURL")):
            raise RuntimeError("Ozon API returned a challenge, not reviews")
        if isinstance(payload.get("reviews"), (list, dict)) or isinstance(
            payload.get("_review_nodes"), list,
        ):
            return
        widgets = payload.get("widgetStates")
        if isinstance(widgets, dict) and any(
            "webListReviews" in str(name)
            or "webReviewProductScore" in str(name)
            for name in widgets
        ):
            return
        if self._extract_review_nodes_from_payload(payload):
            return
        raise RuntimeError("Ozon API returned no review widgets")

    # ------------------------------------------------------------------
    # page_key transition detection
    # ------------------------------------------------------------------
    #
    # Ozon's pagination sometimes hands us a ``nextPage`` URL whose
    # ``page_key`` query parameter differs from the one we just
    # fetched. This typically signals a transition into a different
    # "variant" of the review stream (e.g. with-photo vs no-photo,
    # positive vs negative, sort order change).
    #
    # When that transition happens, Ozon's nextPage URL keeps the old
    # ``page`` and ``layout_page_index`` counters (e.g. page=7) even
    # though the new variant starts at page=1. Fetching page=7 with
    # the new page_key returns 0 reviews and Ozon tells us there is
    # no nextPage — so naive iteration stops short.
    #
    # The fix: when we observe (page_key changed) AND (0 reviews
    # returned), retry the SAME path with ``page=1`` and
    # ``layout_page_index=1``. If that also returns 0 reviews, we
    # really are done. If it returns reviews, we continue iteration
    # from the new variant.
    @staticmethod
    def _extract_query_param(
        path: str,
        name: str,
    ) -> str | None:
        """Extract a single query parameter from a (possibly relative)
        URL path. Returns ``None`` if the parameter is absent."""
        parts = urlsplit(path)
        qs = dict(parse_qsl(parts.query))
        return qs.get(name)

    @staticmethod
    def _reset_page_in_path(
        path: str,
        *,
        page: int = 1,
    ) -> str:
        """Return a copy of ``path`` with both ``page`` and
        ``layout_page_index`` set to ``page``.

        Both parameters always move in lockstep in Ozon's nextPage
        URLs, so resetting them together keeps the URL consistent.
        If either is absent from the original URL it is added.
        """
        parts = urlsplit(path)
        qs = dict(parse_qsl(parts.query, keep_blank_values=True))
        qs["page"] = str(page)
        qs["layout_page_index"] = str(page)
        return urlunsplit(parts._replace(query=urlencode(qs)))

    async def get_ozon_reviews_json(
        self,
        product_path: str,
        *,
        page_number: int = 1,
    ) -> dict[str, Any]:
        async with closing_iterator(
            self.iter_ozon_reviews_json(
                product_path=product_path,
                start_page=page_number,
                max_pages=1,
            )
        ) as stream:
            async for current_page, payload in stream:
                if current_page == 1:
                    return payload

        raise RuntimeError(
            f"Не удалось получить страницу Ozon {page_number}"
        )

    async def iter_ozon_reviews_by_scroll(
            self,
            product_path: str,
            *,
            max_reviews: int | None = None,
            max_rounds: int = 500,
            stable_rounds_limit: int = 3,
            scroll_step: int = 1800,
            pause_ms: int = 1000,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async with self._browser_context() as browser:
            page = await browser.new_page()
            await self._inject_cookies(page)
            await self._install_resource_blocker(page)

            reviews_url = (
                f"{OZON_BASE_URL}"
                f"{product_path}/reviews/"
            )

            await page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            await self._wait_for_reviews_ready(page)

            review_locator = page.locator(
                "[data-review-uuid]"
            )

            await review_locator.first.wait_for(
                state="attached",
                timeout=30_000,
            )

            seen_ids: set[str] = set()
            previous_count = 0
            stable_rounds = 0

            for _round_number in range(1, max_rounds + 1):
                cards = await self._read_review_cards(
                    review_locator,
                )

                new_cards = []

                for card in cards:
                    review_id = card.get("uuid")

                    if not review_id:
                        continue

                    if review_id in seen_ids:
                        continue

                    seen_ids.add(review_id)
                    new_cards.append(card)

                if new_cards:
                    yield new_cards

                current_count = await review_locator.count()

                if (
                        max_reviews is not None
                        and len(seen_ids) >= max_reviews
                ):
                    return

                if current_count > previous_count:
                    previous_count = current_count
                    stable_rounds = 0
                else:
                    stable_rounds += 1

                if stable_rounds >= stable_rounds_limit:
                    return

                await page.mouse.wheel(
                    0,
                    scroll_step,
                )

                await page.wait_for_timeout(
                    pause_ms,
                )

    # ------------------------------------------------------------------
    # Unified "collect ALL reviews" flow
    # ------------------------------------------------------------------
    #
    # The legacy pagination iterator relies on Ozon's internal
    # entrypoint-api.bx endpoint, which is fast but is occasionally
    # blocked by Cloudflare or returns 0 reviews on heavily bot-protected
    # products. The scroll iterator is slower but more resilient because
    # it reads directly from the page DOM.
    #
    # ``iter_all_ozon_reviews`` runs both strategies in sequence with
    # separate browser sessions, and deduplicates review cards
    # by UUID across the two strategies. The result is a single stream
    # that aims to yield every review visible to a real browser user.
    #
    # Strategy order:
    #   1. Pagination — fetch reviews via /api/entrypoint-api.bx/page/json/v2
    #      following nextPage until exhausted.
    #   2. Scroll — open the /reviews/ page and scroll-load all DOM cards.
    #
    # Each yielded item is tagged with the strategy that produced it
    # so the adapter can apply strategy-specific parsing.
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
    ) -> AsyncIterator[
        tuple[str, dict[str, Any]]
    ]:
        """Yield ``(strategy, review_node)`` tuples for every unique review.

        ``strategy`` is either ``"pagination"`` or ``"scroll"``.
        Deduplication is by review UUID across both strategies.
        """
        from shared.logging import get_logger

        log = get_logger("transports.browser_json")

        seen_ids: set[str] = set()

        # ------------------ pagination ------------------
        pagination_yielded = 0
        try:
            async with closing_iterator(
                self.iter_ozon_reviews_json(
                    product_path=product_path,
                    start_page=pagination_start_page,
                    max_pages=pagination_max_pages,
                    retry_attempts=retry_attempts,
                    page_delay_seconds=page_delay_seconds,
                )
            ) as stream:
                async for _page_num, payload in stream:
                    review_nodes = self._extract_review_nodes_from_payload(
                        payload,
                    )

                    for node in review_nodes:
                        rid = self._review_node_id(node)
                        if rid and rid in seen_ids:
                            continue
                        if rid:
                            seen_ids.add(rid)

                        yield "pagination", node
                        pagination_yielded += 1

                        if (
                            max_reviews is not None
                            and len(seen_ids) >= max_reviews
                        ):
                            log.info(
                                "Ozon: reached max_reviews={}, stopping",
                                max_reviews,
                            )
                            return

        except Exception as exc:
            log.warning(
                "Ozon: pagination failed after {} reviews: {} — "
                "falling back to scroll",
                pagination_yielded, exc,
            )

        log.info(
            "Ozon: pagination phase done, {} unique reviews",
            pagination_yielded,
        )

        if max_reviews is not None and len(seen_ids) >= max_reviews:
            return
        if (
            self.last_review_count is not None
            and len(seen_ids) >= self.last_review_count
        ):
            return

        # ------------------ scroll (fallback / supplement) ------------------
        scroll_yielded = 0
        try:
            async with closing_iterator(
                self.iter_ozon_reviews_by_scroll(
                    product_path=product_path,
                    max_reviews=None,
                    max_rounds=scroll_max_rounds,
                    pause_ms=int(scroll_pause_seconds * 1000),
                )
            ) as stream:
                async for batch in stream:
                    for card in batch:
                        rid = card.get("uuid")
                        if rid and rid in seen_ids:
                            continue
                        if rid:
                            seen_ids.add(rid)

                        yield "scroll", card
                        scroll_yielded += 1

                        if (
                            max_reviews is not None
                            and len(seen_ids) >= max_reviews
                        ):
                            log.info(
                                "Ozon: reached max_reviews={}, stopping",
                                max_reviews,
                            )
                            return

        except Exception as exc:
            log.error(
                "Ozon: scroll phase failed after {} reviews: {}",
                scroll_yielded, exc,
            )
            # Re-raise only if pagination also produced nothing —
            # otherwise we still have something useful to return.
            if pagination_yielded == 0:
                raise
            log.warning(
                "Ozon: keeping {} reviews from pagination only",
                pagination_yielded,
            )

        log.info(
            "Ozon: scroll phase done, {} new reviews "
            "(total unique: {})",
            scroll_yielded, len(seen_ids),
        )

    async def _save_debug(
        self,
        *,
        page: Any,
        payload: dict[str, Any],
        page_number: int,
        stream_suffix: str = "",
    ) -> None:
        if not self.debug_dumps:
            return
        page_dir = self.debug_dir / (
            f"page_{page_number}{stream_suffix}"
        )
        page_dir.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )

        (page_dir / "response.json").write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (page_dir / "page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        if self.screenshots:
            await page.screenshot(
                path=str(page_dir / "page.png"),
                full_page=True,
            )
