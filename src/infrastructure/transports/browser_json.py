from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _import_invisible_playwright():
    """Lazy import of invisible-playwright.

    The library is heavy (it pulls in a patched Playwright + Chromium
    binaries) and not available in every environment that imports
    this module (e.g. unit tests of pure-Python helpers). Importing it
    lazily inside the async generators that actually need a browser
    lets the rest of the module — including the constructor and the
    pure-Python ``_fetch_json_with_retry`` / static helpers — work
    without the browser stack installed.
    """
    from invisible_playwright.async_api import InvisiblePlaywright
    return InvisiblePlaywright


def _get_retryable_errors() -> tuple[type[BaseException], ...]:
    """Return the tuple of exception types that should trigger a retry.

    Built dynamically so we can include ``invisible_playwright``'s
    ``Error`` class only when the library is installed. Always
    includes ``RuntimeError`` and the standard ``TimeoutError`` /
    ``asyncio.TimeoutError`` (in Python 3.11+ these are unified, but
    we keep both for safety on 3.10).
    """
    import asyncio as _asyncio

    types: list[type[BaseException]] = [
        RuntimeError,
        TimeoutError,
        _asyncio.TimeoutError,
    ]
    try:
        from invisible_playwright._pw._impl._errors import (
            Error as PlaywrightError,
        )
        types.append(PlaywrightError)
    except ImportError:
        # invisible-playwright not installed — that's OK, the retry
        # still works on RuntimeError and TimeoutError.
        pass

    return tuple(types)


# Module-level cache so we don't re-import on every retry.
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] | None = None


def _retryable_errors() -> tuple[type[BaseException], ...]:
    global _RETRYABLE_ERRORS
    if _RETRYABLE_ERRORS is None:
        _RETRYABLE_ERRORS = _get_retryable_errors()
    return _RETRYABLE_ERRORS


# Stealth init script — patches the most common signals Cloudflare
# uses to detect automated / headless browsers. Adapted from the
# open-source playwright-stealth project (https://github.com/
# Mattwmaster58/playwright_stealth) and tailored for Firefox.
#
# Applied to every fresh page via ``page.add_init_script`` so the
# patches run before any page JS executes.
_STEALTH_INIT_SCRIPT = """
// Hide that we're a WebDriver-controlled browser.
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// Pretend we have the Chrome runtime object that real Chrome
// browsers expose. Some bot detection scripts check for its
// presence.
if (!window.chrome) {
    window.chrome = {
        runtime: {},
        app: {},
        csi: () => {},
        loadTimes: () => {},
    };
}

// Override Notification.permission so it doesn't say "denied" —
// real browsers say "default" until the user has interacted.
if (window.Notification) {
    Object.defineProperty(Notification, 'permission', {
        get: () => 'default',
        configurable: true,
    });
}

// Pretend we have plugins (real browsers have at least PDF viewer).
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {
            name: 'PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
        },
        {
            name: 'Chrome PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
        },
    ],
    configurable: true,
});

// Pretend we have a non-zero set of mime types.
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => [
        {
            type: 'application/pdf',
            suffixes: 'pdf',
            description: 'Portable Document Format',
        },
        {
            type: 'text/pdf',
            suffixes: 'pdf',
            description: 'Portable Document Format',
        },
    ],
    configurable: true,
});

// Make the navigator.languages look real.
Object.defineProperty(navigator, 'languages', {
    get: () => ['ru', 'ru-RU', 'en-US', 'en'],
    configurable: true,
});

// Patch permissions query so it doesn't say "denied" for
// notifications.
const originalQuery = window.navigator.permissions
    ? window.navigator.permissions.query
    : null;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({state: 'default'})
            : originalQuery(parameters)
    );
}

// Make window.outerWidth / outerHeight look non-zero (headless
// browsers often report 0).
if (window.outerWidth === 0 || window.outerHeight === 0) {
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth || 1280,
        configurable: true,
    });
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 85 || 720,
        configurable: true,
    });
}

// Webdriver test: some detection scripts check
// ``window.navigator.webdriver === false`` explicitly. Set it.
try {
    delete Object.getPrototypeOf(navigator).webdriver;
} catch (e) {
    // Some builds don't allow delete on the prototype.
}
"""


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


class BrowserJsonTransport:
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
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.seed = seed
        self.pin = pin
        self.humanize = humanize
        self.stealth = stealth
        if fetch_strategy not in ("navigation", "fetch"):
            raise ValueError(
                f"Unknown fetch_strategy: {fetch_strategy!r}. "
                "Use 'navigation' or 'fetch'."
            )
        self.fetch_strategy = fetch_strategy

    async def iter_ozon_reviews_json(
            self,
            product_path: str,
            *,
            start_page: int = 1,
            max_pages: int | None = None,
            retry_attempts: int = 3,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
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

            # Page factory: always returns a fresh page. Each retry
            # attempt asks for a new page so that a broken execution
            # context (CDP error, "operation aborted") doesn't poison
            # subsequent fetches.
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
            )

            seen_paths: set[str] = set()
            processed_pages = 0
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

                # _goto_with_retry recreates the page on each attempt
                # and returns the page that successfully completed
                # the navigation. We use that same page for the fetch.
                page = await self._goto_with_retry(
                    page_factory=page_factory,
                    reviews_url=reviews_url,
                    attempts=retry_attempts,
                    label=(
                        f"Ozon goto page {processed_pages + 1} "
                        f"({current_path})"
                    ),
                )

                if self.settle_ms > 0:
                    await page.wait_for_timeout(
                        self.settle_ms,
                    )

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
                    page = await page_factory()
                    await page.goto(
                        reviews_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    if self.settle_ms > 0:
                        await page.wait_for_timeout(self.settle_ms)
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

                processed_pages += 1

                await self._save_debug(
                    page=page,
                    payload=payload,
                    page_number=processed_pages,
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

    @staticmethod
    def _build_initial_path(
            *,
            product_path: str,
            page_number: int,
    ) -> str:
        return (
            f"{product_path}/reviews"
            f"?page={page_number}"
        )

    @staticmethod
    def _absolute_url(path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path

        return f"https://www.ozon.ru{path}"

    @staticmethod
    def extract_next_path(
            payload: dict[str, Any],
    ) -> str | None:
        next_page = payload.get("nextPage")

        if isinstance(next_page, str):
            return next_page or None

        if isinstance(next_page, dict):
            for key in ("url", "href", "path"):
                value = next_page.get(key)

                if isinstance(value, str) and value:
                    return value

        return None

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

            reviews_url = (
                f"https://www.ozon.ru"
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

            for round_number in range(1, max_rounds + 1):
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
        from shared.retry import (
            retry_async,
            sleep_with_jitter,
        )

        log = get_logger("transports.browser_json")

        seen_ids: set[str] = set()

        # ------------------ pagination ------------------
        pagination_yielded = 0
        try:
            async for page_num, payload in self.iter_ozon_reviews_json(
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

                if page_delay_seconds > 0:
                    await sleep_with_jitter(
                        page_delay_seconds,
                    )
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

    @staticmethod
    def _extract_review_nodes_from_payload(
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Pull every dict that looks like a review out of a raw
        Ozon pagination payload.

        Reuses ``walk_json`` semantics from the adapter without
        duplicating its fuzzy matcher — this is intentionally a thin
        pass-through that just surfaces raw candidate nodes.
        """
        # Local import to avoid a hard dep cycle with the adapter module.
        from infrastructure.marketplaces.ozon import (
            extract_reviews_from_ozon_payload,
        )
        from domain.entities import ProductRef

        # extract_reviews_from_ozon_payload needs a ProductRef for
        # building Review objects, but we only need the raw node
        # identification logic. Pass a minimal placeholder.
        placeholder = ProductRef(
            marketplace="ozon",
            source_url="",
            product_id="_placeholder",
        )
        reviews = extract_reviews_from_ozon_payload(
            payload, placeholder,
        )
        # ``raw`` field on each Review is the original node dict
        return [r.raw for r in reviews if isinstance(r.raw, dict)]

    @staticmethod
    def _review_node_id(node: dict[str, Any]) -> str | None:
        """Best-effort extraction of a stable id from a review node."""
        for key in (
            "reviewId",
            "review_id",
            "reviewUuid",
            "review_uuid",
            "uuid",
            "id",
        ):
            value = node.get(key)
            if value:
                return str(value)
        return None


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
        endpoint = (
            "https://www.ozon.ru"
            "/api/entrypoint-api.bx/page/json/v2"
        )
        endpoint_url = (
            f"{endpoint}?"
            f"{urlencode({'url': internal_path})}"
        )

        # Navigate directly to the API URL. The browser sends all
        # session cookies and produces a request that Cloudflare
        # cannot distinguish from a real user navigation.
        response = await page.goto(
            endpoint_url,
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
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

        # Read the response body. We have two options:
        #
        # 1. ``response.text()`` — the raw HTTP response body as
        #    received by the browser, before any rendering. This is
        #    the only reliable way to get JSON when the browser
        #    renders it through its built-in JSON viewer (Firefox
        #    shows a tree UI for ``application/json`` URLs, and
        #    ``document.body.textContent`` returns the viewer text,
        #    not the raw JSON).
        #
        # 2. ``page.evaluate(document.body.textContent)`` — what
        #    the rendered DOM looks like. Used as a fallback when
        #    ``response`` is None (e.g. when Cloudflare returns a
        #    redirect chain and the final response isn't the one
        #    ``page.goto`` returned).
        body = ""
        if response is not None:
            try:
                body = await response.text() or ""
            except Exception:
                body = ""

        if not body:
            # Fallback: read from the rendered DOM (works when
            # ``response.text()`` is unavailable or when the body
            # was already rendered into <pre> by a non-JSON-viewer
            # browser).
            body = await self._read_page_body(page)

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
        endpoint = (
            "https://www.ozon.ru"
            "/api/entrypoint-api.bx/page/json/v2"
        )

        endpoint_url = (
            f"{endpoint}?"
            f"{urlencode({'url': internal_path})}"
        )

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
    ) -> Any:
        """Wrap ``page.goto`` with exponential-backoff retry.

        ``page.goto`` can fail with the same family of Playwright errors
        as ``page.evaluate`` ("The operation was aborted", navigation
        timeout, CDP connection drop). When that happens we close the
        current page and ask ``page_factory()`` for a fresh one, then
        retry the goto on the new page. This survives "execution
        context lost" errors that would otherwise kill the whole
        pagination stream.
        """
        if attempts <= 1:
            page = await page_factory()
            await page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            return page

        from shared.retry import retry_async

        async def _goto_once() -> Any:
            # Always create a fresh page on each attempt — the
            # previous one is likely in a broken state if we got here.
            page = await page_factory()
            await page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            return page

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

    @staticmethod
    def _is_cloudflare_challenge(body: str) -> bool:
        """Heuristic for detecting a Cloudflare challenge response body.

        Two known shapes:

        1. JSON envelope (older API-level challenge):
           ``{"incidentId": "fab_chlg_...", "challengeURL": "..."}``

        2. HTML "Browser Challenge" page (Cloudflare Under-Attack
           interstitial):
           Contains ``Пожалуйста, включите JavaScript`` /
           ``enable JavaScript to continue`` /
           ``We need to make sure that you are not a robot`` /
           an ``ID: fab_chlg_...`` line.
        """
        if not body:
            return False
        body_lower = body.lower()
        return (
            "challengeurl" in body_lower
            or "incidentid" in body_lower
            or "challenge.html" in body_lower
            # HTML challenge page markers
            or "fab_chlg_" in body_lower
            or "enable javascript" in body_lower
            or "включите javascript" in body_lower
            or "we need to make sure that you are not a robot"
            in body_lower
            or "нам нужно убедиться, что вы не робот"
            in body_lower
        )

    async def _save_debug(
        self,
        *,
        page,
        payload: dict[str, Any],
        page_number: int,
    ) -> None:
        page_dir = self.debug_dir / f"page_{page_number}"
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

        await page.screenshot(
            path=str(page_dir / "page.png"),
            full_page=True,
        )

    @staticmethod
    def _absolute_url(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path

        return f"https://www.ozon.ru{path}"

    @staticmethod
    def _build_initial_path(
        *,
        product_path: str,
        page_number: int,
    ) -> str:
        query = urlencode({"page": page_number})
        return f"{product_path}/reviews?{query}"

    @staticmethod
    def extract_next_path(
        payload: dict[str, Any],
    ) -> str | None:
        next_page = payload.get("nextPage")

        if isinstance(next_page, str):
            return next_page or None

        if isinstance(next_page, dict):
            for key in ("url", "href", "path"):
                value = next_page.get(key)

                if isinstance(value, str) and value:
                    return value

        return None
