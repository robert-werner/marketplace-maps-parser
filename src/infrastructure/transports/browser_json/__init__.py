from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from infrastructure.transports.base import (
    OZON_BASE_URL,
    OzonTransportMixin,
)
from infrastructure.transports.browser_common import (
    _STEALTH_INIT_SCRIPT as _STEALTH_INIT_SCRIPT,
)

# Retry semantics and the stealth script are shared with the other
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


def _import_invisible_playwright() -> type:
    """Back-compat shim: the lazy factory moved to
    browser_common.import_invisible_playwright (tests patch the
    module attribute by this name)."""
    return import_invisible_playwright()





from infrastructure.transports.browser_json._dom import CardReadingMixin
from infrastructure.transports.browser_json._errors import CloudflareChallengeError
from infrastructure.transports.browser_json._fetch import FetchMixin


class BrowserJsonTransport(FetchMixin, CardReadingMixin, OzonTransportMixin):
    """Получает JSON Ozon в одной browser-сессии.

    Сначала открывается страница отзывов, затем внутренний endpoint
    вызывается из этой же страницы через window.fetch(). Следующая
    страница берётся из поля nextPage ответа Ozon.

    Two fetch strategies are supported (controlled by
    ``fetch_strategy`` constructor arg):

    - ``"navigation"`` (default): ``page.goto(api_url)`` — opens the
      API URL directly in the browser tab. Cloudflare sees a real
      browser navigation and is much less likely to return 403.
    - ``"fetch"`` (legacy): ``page.evaluate(fetch(api_url))`` — calls
      ``fetch()`` from the page's JS context. Faster but Cloudflare
      blocks it more aggressively.

    Stealth mode (``stealth=True`` by default) applies an init
    script to every fresh page that patches ``navigator.webdriver``,
    ``chrome.runtime``, ``Notification.permission``, and other
    signals Cloudflare uses to detect automated browsers.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 2_000,
        debug_dir: str = "debug_ozon",
        proxy: dict[str, str] | None = None,
        seed: int | None = None,
        pin: dict[str, Any] | None = None,
        humanize: bool = True,
        fetch_strategy: str = "navigation",
        stealth: bool = True,
        # Playwright-format cookies of a logged-in Ozon session
        # (see cookie_loader). Injected into every page before the
        # first navigation — the internal API needs them to serve
        # the full review list instead of the anonymous subset.
        cookies: list[dict[str, Any]] | None = None,
        # Save a page screenshot into the debug dir on every debug
        # dump. OFF by default: full-page screenshots of a logged-in
        # session are a PII hazard and cost a noticeable share of the
        # per-page wall time. HTML/payload dumps stay on.
        screenshots: bool = False,
        # Abort image/font/media requests on scraper navigations.
        # Review photos dominate the ~880KB reviews page; the JSON
        # fetch needs none of them, so blocking cuts the per-page
        # wall time roughly in half. Mirrors the public_page
        # transport (same measured pattern: route by file extension,
        # never "**/*").
        block_assets: bool = True,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.seed = seed
        self.pin = pin
        self.humanize = humanize
        self.stealth = stealth
        self.cookies = cookies
        self.screenshots = screenshots
        self.block_assets = block_assets
        if fetch_strategy not in ("navigation", "fetch"):
            raise ValueError(
                f"Unknown fetch_strategy: {fetch_strategy!r}. "
                "Use 'navigation' or 'fetch'."
            )
        self.fetch_strategy = fetch_strategy

    async def _inject_cookies(self, page) -> None:
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

    async def _settle_after_goto(self, page) -> None:
        """Wait after the reviews-HTML goto, before the JSON fetch.

        - ``fetch`` strategy: the full ``settle_ms`` — the page's JS
          context must be warm before ``fetch()`` runs from it.
        - ``navigation`` strategy: a short fixed grace (500ms) — the
          goto to the API URL replaces the page content anyway, so a
          long settle is pure waste; the grace only lets the page's
          antibot beacons fire.
        """
        if self.fetch_strategy == "fetch":
            if self.settle_ms > 0:
                await page.wait_for_timeout(self.settle_ms)
            return
        await page.wait_for_timeout(500)

    async def iter_ozon_reviews_json(
            self,
            product_path: str,
            *,
            start_page: int = 1,
            max_pages: int | None = None,
            retry_attempts: int = 3,
            extra_query: str = "",
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Paginate the internal Ozon reviews API.

        ``extra_query`` appends stream-variant parameters to the
        FIRST page URL (e.g. ``&sort=score_asc``); subsequent pages
        follow Ozon's ``nextPage`` verbatim, so the variant applies
        to the whole stream when Ozon echoes it in nextPage.
        """
        self.debug_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with _import_invisible_playwright()(
                proxy=self.proxy,
                seed=self.seed,
                pin=self.pin,
                humanize=self.humanize,
        ) as browser:

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
                await self._inject_cookies(new_page)
                await self._install_resource_blocker(new_page)
                # Apply stealth init script to every fresh page. This
                # patches ``navigator.webdriver``, ``chrome.runtime``,
                # ``Notification.permission``, ``window.outerWidth`` /
                # ``window.outerHeight`` and other signals that
                # Cloudflare uses to detect headless / automated
                # browsers. See ``_STEALTH_INIT_SCRIPT`` for the full
                # patch list.
                if self.stealth:
                    try:
                        await new_page.add_init_script(
                            _STEALTH_INIT_SCRIPT,
                        )
                    except Exception as exc:
                        # Don't fail hard — invisible-playwright may
                        # not support add_init_script in some builds.
                        print(
                            "Ozon: warning — не удалось применить "
                            f"stealth init script: {exc}"
                        )
                current_page_holder["page"] = new_page
                return new_page

            page = await page_factory()

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

                reviews_url = self._absolute_url(current_path)

                # _goto_with_retry reuses the current tab on the
                # first attempt (one user-like tab per stream) and
                # falls back to a fresh page on retries.
                page = await self._goto_with_retry(
                    page_factory=page_factory,
                    page=page,
                    reviews_url=reviews_url,
                    attempts=retry_attempts,
                    label=(
                        f"Ozon goto page {processed_pages + 1} "
                        f"({current_path})"
                    ),
                )

                await self._settle_after_goto(page)

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
                        print(
                            f"Ozon: second retry also failed on page "
                            f"{processed_pages + 1}: {exc2}; "
                            f"останавливаю сбор для избежания пропусков"
                        )
                        return

                processed_pages += 1

                await self._save_debug(
                    page=page,
                    payload=payload,
                    page_number=processed_pages,
                    stream_suffix=stream_suffix,
                )

                print(
                    "nextPage:",
                    payload.get("nextPage"),
                )
                print(
                    "pageInfo:",
                    payload.get("pageInfo"),
                )
                print(
                    "pageToken:",
                    payload.get("pageToken"),
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
                        next_num = int(current_page_num) + 1
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
        async for current_page, payload in (
            self.iter_ozon_reviews_json(
                product_path=product_path,
                start_page=page_number,
                max_pages=1,
            )
        ):
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
        async with _import_invisible_playwright()(
                proxy=self.proxy,
                seed=self.seed,
                pin=self.pin,
                humanize=self.humanize,
        ) as browser:
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

            if self.settle_ms > 0:
                await page.wait_for_timeout(
                    self.settle_ms,
                )

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
    # ``iter_all_ozon_reviews`` runs BOTH strategies in sequence, in a
    # single Playwright browser session, and deduplicates review cards
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
        from shared.pacing import AdaptivePacer

        log = get_logger("transports.browser_json")

        seen_ids: set[str] = set()

        # Adaptive inter-page pacing (see shared.pacing): shrinks
        # the delay after clean pages, backs off on challenge
        # events reported via _notify_pacer_block.
        self._pacer: AdaptivePacer | None = (
            AdaptivePacer(base_delay=page_delay_seconds)
            if page_delay_seconds > 0
            else None
        )

        # ------------------ pagination ------------------
        pagination_yielded = 0
        try:
            async for _page_num, payload in self.iter_ozon_reviews_json(
                product_path=product_path,
                start_page=pagination_start_page,
                max_pages=pagination_max_pages,
                retry_attempts=retry_attempts,
            ):
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

                if self._pacer is not None:
                    self._pacer.record_success()
                    await self._pacer.wait()
        except Exception as exc:
            log.warning(
                "Ozon: pagination failed after {} reviews: {} — "
                "falling back to scroll",
                pagination_yielded, exc,
            )
        finally:
            self._pacer = None

        log.info(
            "Ozon: pagination phase done, {} unique reviews",
            pagination_yielded,
        )

        if max_reviews is not None and len(seen_ids) >= max_reviews:
            return

        # ------------------ scroll (fallback / supplement) ------------------
        scroll_yielded = 0
        try:
            async for batch in self.iter_ozon_reviews_by_scroll(
                product_path=product_path,
                max_reviews=None,
                max_rounds=scroll_max_rounds,
                pause_ms=int(scroll_pause_seconds * 1000),
            ):
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
        page,
        payload: dict[str, Any],
        page_number: int,
        stream_suffix: str = "",
    ) -> None:
        page_dir = self.debug_dir / (
            f"page_{page_number}{stream_suffix}"
        )
        page_dir.mkdir(
            parents=True,
            exist_ok=True,
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
