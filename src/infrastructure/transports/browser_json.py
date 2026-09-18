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




class CloudflareChallengeError(RuntimeError):
    """Raised when Ozon returns HTTP 403 with a Cloudflare
    ``challenge.html`` body instead of the expected JSON.

    Triggers a much longer backoff than a generic transient error:
    Cloudflare expects clients to wait 10+ seconds between
    challenge-failed retries, otherwise it keeps returning the
    challenge indefinitely. Subclasses RuntimeError so existing
    retry_on filters that include RuntimeError still catch it.
    """

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        # Truncate the body so log lines stay readable — the
        # challenge body is a long base64-ish blob.
        preview = body[:200] + "..." if len(body) > 200 else body
        super().__init__(
            f"Cloudflare challenge (HTTP {status}) on {url}: "
            f"body={preview}"
        )


class BrowserJsonTransport(OzonTransportMixin):
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

    # ------------------------------------------------------------------
    # Per-page speed helpers
    # ------------------------------------------------------------------
    #
    # Images/fonts/media are the bulk of the reviews page's bytes
    # (~880KB); the JSON fetch needs none of them. Same measured
    # pattern as public_page: route by file extension, never
    # "**/*" (routing all ~200 requests through Python costs more
    # than the blocked assets save).
    _BLOCKED_RESOURCE_TYPES = frozenset(
        {"image", "font", "media"}
    )
    _ASSET_ROUTE_PATTERNS = (
        "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.webp",
        "**/*.gif", "**/*.avif", "**/*.woff", "**/*.woff2",
        "**/*.ttf", "**/*.mp4",
    )

    async def _install_resource_blocker(self, page) -> None:
        if not self.block_assets:
            return

        async def _route(route):
            try:
                if (
                    route.request.resource_type
                    in self._BLOCKED_RESOURCE_TYPES
                ):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        for pattern in self._ASSET_ROUTE_PATTERNS:
            try:
                # invisible-playwright's Page.route is a coroutine —
                # calling it without await silently drops the route.
                await page.route(pattern, _route)
            except Exception:
                # Fakes / builds without routing support: fall back
                # to loading assets (slower but correct).
                pass

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

    async def _read_review_cards(
            self,
            review_locator,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        for index in range(await review_locator.count()):
            card = review_locator.nth(index)

            result.append(
                {
                    "uuid": await card.get_attribute(
                        "data-review-uuid"
                    ),
                    "published_at": await card.get_attribute(
                        "publishedat"
                    ),
                    "status_id": await card.get_attribute(
                        "statusid"
                    ),
                    "text": await card.inner_text(),
                    "rating": await self._read_review_rating(card),
                    "images": await self._read_images(card),
                }
            )

        return result

    async def _read_review_rating(self, card) -> int | None:
        """Read the per-review star rating from the DOM card.

        Each star is an SVG; the filled vs unfilled star has a
        different computed color (yellow vs grey). We count the
        yellow (filled) stars. The selector matches the rating
        container that Ozon wraps around the stars.

        Mirrors ``BrowserDomTransport._read_review_rating`` — kept
        duplicated (not shared) to avoid coupling the two transport
        classes together.
        """
        rating_container = card.locator(
            '[class*="rpProducta9c"]'
        )

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
        """Heuristic for deciding whether a star SVG is filled yellow
        (counted) or unfilled grey (not counted). Mirrors
        ``BrowserDomTransport._is_filled_star``.
        """
        if not color:
            return False

        values = " ".join(
            str(value).lower()
            for value in color.values()
            if value is not None
        )

        # Yellow star markers — Ozon may change the exact RGB.
        yellow_markers = (
            "rgb(255, 198, 0)",
            "rgb(255, 198, 51)",
            "#ffc600",
            "#ffc633",
            "#ffce00",
            "ffc600",
            "ffc633",
            "ffce00",
        )
        if any(marker in values for marker in yellow_markers):
            return True

        # Class-based marker used by some Ozon layouts.
        if "filled" in values and "empty" not in values:
            return True

        return False

    async def _read_images(
            self,
            card,
    ) -> list[str]:
        result: list[str] = []
        images = card.locator("img")

        for index in range(await images.count()):
            src = await images.nth(index).get_attribute(
                "src"
            )

            if src:
                result.append(src)

        return result

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

    async def _fetch_json_inside_page(
        self,
        *,
        page,
        internal_path: str,
    ) -> dict[str, Any]:
        """Fetch the Ozon reviews JSON for ``internal_path``.

        Dispatches to one of two strategies based on
        ``self.fetch_strategy``:

        - ``"navigation"`` (default): ``page.goto(api_url)`` —
          navigate the page directly to the API URL. Cloudflare
          treats this as a real browser navigation and is much
          less likely to return 403.
        - ``"fetch"``: ``page.evaluate(fetch(api_url))`` — call
          ``fetch()`` from the page's JS context. Faster but
          Cloudflare blocks it more aggressively.
        """
        if self.fetch_strategy == "fetch":
            return await self._fetch_json_inside_page_via_fetch(
                page=page,
                internal_path=internal_path,
            )
        return await self._fetch_json_via_navigation(
            page=page,
            internal_path=internal_path,
        )

    async def _fetch_json_via_navigation(
        self,
        *,
        page,
        internal_path: str,
    ) -> dict[str, Any]:
        """Fetch Ozon reviews JSON by navigating the page directly
        to the API endpoint.

        Cloudflare's bot detection distinguishes between real browser
        navigations (page.goto) and in-page JS fetch() calls. The
        former pass through cleanly because they look like a user
        clicking a link; the latter often get 403 with a challenge
        body.

        When Cloudflare returns its HTML "Browser Challenge" page
        (``Пожалуйста, включите JavaScript``), the embedded JS needs
        time to execute, submit the challenge token, and redirect
        to the actual JSON. We detect that page and wait for the
        body to change before reading the final response.
        """
        endpoint_url = self._build_api_url(internal_path)

        # Navigate directly to the API URL. The browser sends all
        # session cookies and produces a request that Cloudflare
        # cannot distinguish from a real user navigation.
        response, body = await self._goto_and_read_body(
            page=page,
            endpoint_url=endpoint_url,
        )

        # ----------------------------------------------------------
        # Fast re-navigation on a lost body.
        # ----------------------------------------------------------
        # The FIRST navigation to the API URL sometimes loses the
        # response body: ``response.text()`` comes back empty and the
        # DOM fallback then reads the Firefox JSON-viewer's UI text
        # instead of the raw JSON. Empirically (logs 2026-09-16) the
        # SECOND navigation of the same URL on the same tab returns
        # the body — today that recovery costs a full retry_async
        # cycle (3-8s backoff per page). Doing the re-navigation
        # IMMEDIATELY, without backoff, removes the most frequent
        # slow-retry source. Challenge pages are excluded: they need
        # waiting, not re-navigation.
        if (
            not body.lstrip().startswith(("{", "["))
            and not self._is_cloudflare_challenge(body)
        ):
            response, body = await self._goto_and_read_body(
                page=page,
                endpoint_url=endpoint_url,
            )

        # Extract response metadata via the Playwright response
        # object (more reliable than parsing document headers).
        status = 0
        response_url = endpoint_url
        content_type = ""

        if response is not None:
            try:
                status = response.status
            except Exception:
                status = 0
            try:
                response_url = response.url
            except Exception:
                response_url = endpoint_url
            try:
                content_type = (
                    response.headers.get("content-type", "") or ""
                ).lower()
            except Exception:
                content_type = ""

        # ----------------------------------------------------------
        # Cloudflare JS challenge handling
        # ----------------------------------------------------------
        # Cloudflare's "Browser Challenge" page contains JS that
        # automatically solves a proof-of-work challenge and
        # redirects to the actual URL. With ``wait_until="domcontent-
        # loaded"``, ``page.goto`` returns the moment the challenge
        # HTML loads — before the JS has time to execute and submit
        # the challenge. We detect that case and wait for the body
        # to change.
        if self._is_cloudflare_challenge(body) or (
            status == 403 and self._is_cloudflare_challenge(body)
        ):
            body, status, response_url, content_type = (
                await self._wait_for_challenge_completion(
                    page=page,
                    endpoint_url=endpoint_url,
                    initial_body=body,
                    initial_status=status,
                    initial_url=response_url,
                    initial_content_type=content_type,
                )
            )

        body = body or ""

        if status == 0:
            # Some Playwright responses don't expose status (e.g.
            # when the page is served from cache). Treat as 200 if
            # the body looks like JSON, otherwise fail.
            if body.lstrip().startswith(("{", "[")):
                status = 200
            else:
                raise RuntimeError(
                    "Ozon navigation fetch: нет HTTP статуса и "
                    f"body не JSON: body={body[:200]}"
                )

        if status < 200 or status >= 300:
            if status == 403 and self._is_cloudflare_challenge(body):
                self._notify_pacer_block()
                raise CloudflareChallengeError(
                    status=status,
                    url=response_url,
                    body=body,
                )
            raise RuntimeError(
                "Ozon navigation fetch завершился ошибкой: "
                f"HTTP {status}; url={response_url}; "
                f"body={body[:1000]}"
            )

        if not content_type:
            # If we couldn't read content-type from the response
            # headers, fall back to body inspection.
            stripped = body.lstrip()
            if stripped.startswith(("{", "[")):
                content_type = "application/json"
            else:
                content_type = "text/html"

        if "json" not in content_type and not body.lstrip().startswith(
            ("{", "[")
        ):
            raise RuntimeError(
                "Ozon navigation fetch вернул не JSON: "
                f"content-type={content_type}; "
                f"body={body[:500]}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Не удалось декодировать JSON Ozon: {body[:500]}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Корень JSON Ozon не является dict"
            )

        return payload

    async def _goto_and_read_body(
        self,
        *,
        page,
        endpoint_url: str,
    ) -> tuple[Any, str]:
        """Navigate to the API URL and read the response body.

        Body reading has two layers:

        1. ``response.text()`` — the raw HTTP response body as
           received by the browser, before any rendering. This is
           the only reliable way to get JSON when the browser
           renders it through its built-in JSON viewer (Firefox
           shows a tree UI for ``application/json`` URLs, and
           ``document.body.textContent`` returns the viewer text,
           not the raw JSON).
        2. ``page.evaluate(<pre>/body)`` — the rendered DOM, used
           when the response object loses its body (Firefox/JUGGLER
           navigation quirk: ``response.text()`` returns empty right
           after the JSON viewer takes over).

        Returns ``(response, body)``; ``response`` may be None and
        ``body`` may be empty — callers decide what to do with them.
        """
        response = await page.goto(
            endpoint_url,
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )

        body = ""
        if response is not None:
            try:
                body = await response.text() or ""
            except Exception:
                body = ""

        if not body:
            body = await self._read_page_body(page)

        return response, body

    async def _read_page_body(self, page) -> str:
        """Read the rendered page's body text.

        Ozon's API returns raw JSON which browsers render inside a
        ``<pre>`` element. We prefer ``<pre>`` over ``document.body``
        because some browsers add whitespace/annotations to the
        body text.
        """
        try:
            return await page.evaluate(
                """
                () => {
                    const pre = document.querySelector("pre");
                    if (pre) {
                        return pre.textContent || "";
                    }
                    return document.body
                        ? (document.body.textContent || "")
                        : "";
                }
                """
            ) or ""
        except Exception as exc:
            raise RuntimeError(
                "Ozon navigation fetch: не удалось прочитать тело "
                f"ответа: {exc}"
            ) from exc

    async def _wait_for_challenge_completion(
        self,
        *,
        page,
        endpoint_url: str,
        initial_body: str,
        initial_status: int,
        initial_url: str,
        initial_content_type: str,
        max_wait_seconds: int = 30,
    ) -> tuple[str, int, str, str]:
        """Wait for a Cloudflare JS challenge page to auto-resolve.

        Cloudflare's challenge HTML contains embedded JavaScript
        that solves a proof-of-work challenge and then redirects
        (via form submission) to the original URL. After the
        redirect, the browser loads the actual JSON response.

        We poll ``page.evaluate`` to read the body every 500ms. Once
        the body no longer looks like a challenge page (or starts
        looking like JSON), we stop waiting and return the new
        body / status / url / content-type.

        If the challenge doesn't resolve within ``max_wait_seconds``,
        we return the initial values (so the caller raises
        ``CloudflareChallengeError``).
        """
        import asyncio

        print(
            "Ozon: обнаружена Cloudflare challenge страница; "
            f"жду до {max_wait_seconds}s завершения JS challenge..."
        )

        deadline = asyncio.get_event_loop().time() + max_wait_seconds
        poll_interval = 0.5

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)

            # Check if the URL changed (Cloudflare redirects after
            # challenge completion).
            try:
                current_url = page.url
            except Exception:
                current_url = initial_url

            # Read the current body
            try:
                current_body = await self._read_page_body(page)
            except Exception:
                # Page might be navigating — try again
                continue

            # Challenge resolved?
            if not self._is_cloudflare_challenge(current_body):
                # The body changed. If it looks like JSON, we're done.
                stripped = current_body.lstrip()
                if stripped.startswith(("{", "[")):
                    print(
                        "Ozon: Cloudflare challenge решён, "
                        "получен JSON ответ"
                    )
                    # We don't have a reliable status for the
                    # post-challenge response — use 200 as the
                    # body is clearly JSON.
                    return (
                        current_body,
                        200,
                        current_url,
                        "application/json",
                    )
                # Body changed but isn't JSON — could be the actual
                # HTML page (e.g. an error page). Stop waiting and
                # let the caller decide.
                print(
                    "Ozon: Cloudflare challenge страница исчезла, "
                    "но ответ не JSON; возвращаю body как есть"
                )
                return (
                    current_body,
                    200,  # assume 200 since challenge resolved
                    current_url,
                    "",
                )

        # Timed out — return the initial challenge body so the
        # caller raises CloudflareChallengeError.
        print(
            f"Ozon: Cloudflare challenge не решена за "
            f"{max_wait_seconds}s; возвращаю challenge body для retry"
        )
        return (
            initial_body,
            initial_status,
            initial_url,
            initial_content_type,
        )

    async def _fetch_json_inside_page_via_fetch(
        self,
        *,
        page,
        internal_path: str,
    ) -> dict[str, Any]:
        """Legacy fetch strategy: call ``fetch()`` from the page's
        JS context.

        Kept for fallback / comparison. Cloudflare blocks this much
        more aggressively than the direct-navigation strategy.
        """
        endpoint_url = self._build_api_url(internal_path)

        result = await page.evaluate(
            """
            async (endpointUrl) => {
                const response = await fetch(endpointUrl, {
                    method: "GET",
                    credentials: "include",
                    headers: {
                        "Accept": "application/json"
                    }
                });

                return {
                    status: response.status,
                    url: response.url,
                    contentType:
                        response.headers.get("content-type") || "",
                    body: await response.text()
                };
            }
            """,
            endpoint_url,
        )

        status = result["status"]
        response_url = result["url"]
        content_type = result["contentType"].lower()
        body = result["body"]

        if status < 200 or status >= 300:
            if status == 403 and self._is_cloudflare_challenge(body):
                self._notify_pacer_block()
                raise CloudflareChallengeError(
                    status=status,
                    url=response_url,
                    body=body,
                )
            raise RuntimeError(
                "Ozon browser fetch завершился ошибкой: "
                f"HTTP {status}; url={response_url}; "
                f"body={body[:1000]}"
            )

        if "json" not in content_type:
            raise RuntimeError(
                "Ozon browser fetch вернул не JSON: "
                f"content-type={content_type}; "
                f"body={body[:500]}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Не удалось декодировать JSON Ozon: {body[:500]}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Корень JSON Ozon не является dict"
            )

        return payload

    async def _goto_with_retry(
        self,
        *,
        page_factory,
        reviews_url: str,
        attempts: int = 3,
        label: str = "Ozon goto",
        page: Any = None,
    ) -> Any:
        """Wrap ``page.goto`` with exponential-backoff retry.

        When ``page`` is given, the FIRST attempt navigates that
        existing tab (a stream then looks like a user paging through
        reviews in one tab, and we skip the new_page/init-script
        cost); every retry — and every call without ``page`` — asks
        ``page_factory()`` for a fresh one. This survives "execution
        context lost" errors that would otherwise kill the whole
        pagination stream: the broken tab is simply replaced.

        ``page.goto`` can fail with the same family of Playwright errors
        as ``page.evaluate`` ("The operation was aborted", navigation
        timeout, CDP connection drop). When that happens we close the
        current page and ask ``page_factory()`` for a fresh one, then
        retry the goto on the new page.
        """
        from shared.retry import retry_async

        reusable_used = False

        async def _goto_once() -> Any:
            nonlocal reusable_used
            if page is not None and not reusable_used:
                # First attempt: reuse the caller's tab.
                reusable_used = True
                target = page
            else:
                # Retries (or no reusable page): fresh page — the
                # previous one is likely in a broken state.
                target = await page_factory()
            await target.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            return target

        if attempts <= 1:
            return await _goto_once()

        return await retry_async(
            _goto_once,
            attempts=attempts,
            base_delay=2.0,
            max_delay=20.0,
            factor=2.0,
            jitter=0.3,
            retry_on=_retryable_errors(),
            label=label,
        )

    async def _fetch_json_with_retry(
        self,
        *,
        page,
        internal_path: str,
        attempts: int = 3,
        label: str = "Ozon fetch",
    ) -> dict[str, Any]:
        """Wrap ``_fetch_json_inside_page`` with exponential-backoff retry.

        Retries on RuntimeError (Cloudflare non-200/non-JSON) and on
        Playwright ``Error`` ("Page.evaluate: The operation was
        aborted", navigation timeouts, CDP connection drops).

        ``CloudflareChallengeError`` gets a longer backoff (10s base,
        60s max) because Cloudflare expects long waits between
        challenge-failed retries. Other ``RuntimeError`` subclasses
        get the standard 1.5s/15s backoff.

        Note: the ``page`` argument here is the same page object across
        all attempts. If the page itself becomes unhealthy (the
        Playwright execution context is lost), this retry may still
        fail repeatedly. For navigation-level retries (``page.goto``)
        we use ``_goto_with_retry`` instead, which recreates the page
        between attempts.
        """
        if attempts <= 1:
            return await self._fetch_json_inside_page(
                page=page,
                internal_path=internal_path,
            )

        from shared.retry import retry_async

        # CloudflareChallengeError is a RuntimeError subclass, so
        # retry_on=_retryable_errors() catches it automatically. We
        # use a moderately longer base delay (3s) and cap (30s) than
        # the original 1.5s/15s so Cloudflare challenges don't get
        # immediately re-fired (which makes Cloudflare more
        # suspicious, not less).
        return await retry_async(
            lambda: self._fetch_json_inside_page(
                page=page,
                internal_path=internal_path,
            ),
            attempts=attempts,
            base_delay=3.0,
            max_delay=30.0,
            factor=2.0,
            jitter=0.3,
            retry_on=_retryable_errors(),
            label=label,
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

