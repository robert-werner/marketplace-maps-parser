from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


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

                await page.goto(
                    reviews_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
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

    async def _fetch_json_with_retry(
        self,
        *,
        page,
        internal_path: str,
        attempts: int = 3,
        label: str = "Ozon fetch",
    ) -> dict[str, Any]:
        """Wrap ``_fetch_json_inside_page`` with exponential-backoff retry.

        Retries only on ``RuntimeError`` — the kind of error raised by
        ``_fetch_json_inside_page`` when Cloudflare returns a non-200,
        non-JSON, or unparseable response. Other exceptions (network
        timeouts, page navigation errors) propagate immediately because
        they typically indicate the browser session is unhealthy and
        a retry on the same page would not help.
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
            retry_on=(RuntimeError,),
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
