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


class BrowserJsonTransport:
    """Получает JSON Ozon в одной browser-сессии.

    Сначала открывается страница отзывов, затем внутренний endpoint
    вызывается из этой же страницы через window.fetch(). Следующая
    страница берётся из поля nextPage ответа Ozon.
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
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.seed = seed
        self.pin = pin
        self.humanize = humanize

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
            page = await browser.new_page()

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

                await self._goto_with_retry(
                    page=page,
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
                    "images": await self._read_images(card),
                }
            )

        return result

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
        page,
        reviews_url: str,
        attempts: int = 3,
        label: str = "Ozon goto",
    ) -> None:
        """Wrap ``page.goto`` with exponential-backoff retry.

        ``page.goto`` can fail with the same family of Playwright errors
        as ``page.evaluate`` ("The operation was aborted", navigation
        timeout, CDP connection drop). Retrying here lets us survive
        transient browser hiccups without losing the whole pagination
        stream.
        """
        if attempts <= 1:
            await page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            return

        from shared.retry import retry_async

        async def _goto_once() -> None:
            await page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

        await retry_async(
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

        Retries on:

        - ``RuntimeError`` — raised by ``_fetch_json_inside_page`` when
          Cloudflare returns a non-200, non-JSON, or unparseable response.
        - ``PlaywrightError`` — raised by invisible-playwright when the
          browser aborts an operation ("Page.evaluate: The operation
          was aborted"), the page navigation times out, or the CDP
          connection drops. These are typically transient and a retry
          on the same page (or with a fresh page) succeeds.
        - ``TimeoutError`` / ``asyncio.TimeoutError`` — same family.

        Other exceptions propagate immediately.
        """
        if attempts <= 1:
            return await self._fetch_json_inside_page(
                page=page,
                internal_path=internal_path,
            )

        from shared.retry import retry_async

        return await retry_async(
            lambda: self._fetch_json_inside_page(
                page=page,
                internal_path=internal_path,
            ),
            attempts=attempts,
            base_delay=1.5,
            max_delay=15.0,
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
