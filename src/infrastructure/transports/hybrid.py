"""Hybrid transport: curl_cffi first, fallback to Playwright.

Production motivation: Cloudflare's bot detection on Ozon has two
layers:

1. **TLS fingerprint** (JA3/JA4 hash) — Cloudflare blocks requests
   whose TLS fingerprint doesn't match a real browser. curl_cffi
   with ``impersonate='chrome120'`` produces the exact same
   fingerprint as a real Chrome 120 client.

2. **JS challenge** — Cloudflare serves an HTML page with embedded
   JavaScript that solves a proof-of-work challenge and gets a
   ``cf_clearance`` cookie. curl_cffi has no JS engine and cannot
   solve these challenges.

The hybrid transport gets the best of both worlds:

- **First, try curl_cffi** — fast, low memory, real TLS fingerprint.
  For most requests this is enough: warmup with the product page
  gets the ``__cf_bm`` cookie, and the API endpoint is accessible.

- **If curl_cffi gets a CloudflareChallengeError** that retry
  exhaustion can't overcome, fall back to Playwright. Playwright
  runs a real browser that solves the JS challenge, gets
  ``cf_clearance``, and then makes the same API request from inside
  the page context.

The fallback is per-page: once Playwright succeeds on a page, we
retry the next page with curl_cffi again (since Cloudflare cookies
are session-bound, and the curl_cffi session is separate). This
means hybrid is faster than pure Playwright when Cloudflare only
intermittently challenges (most pages succeed via curl_cffi).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from infrastructure.transports.base import OzonTransportMixin


def _import_curl_cffi_transport():
    """Lazy import to avoid coupling at module load time."""
    from infrastructure.transports.curl_cffi import CurlCffiTransport
    return CurlCffiTransport


def _import_browser_json_transport():
    from infrastructure.transports.browser_json import (
        BrowserJsonTransport,
    )
    return BrowserJsonTransport


def _import_cloudflare_error():
    from infrastructure.transports.browser_json import (
        CloudflareChallengeError,
    )
    return CloudflareChallengeError


def _import_retry_async():
    from shared.retry import retry_async
    return retry_async


class HybridTransport(OzonTransportMixin):
    """Hybrid curl_cffi + Playwright transport.

    Tries curl_cffi first (fast, low memory, real TLS fingerprint).
    On persistent CloudflareChallengeError, falls back to
    Playwright (slow, full browser, can solve JS challenges).

    Constructor args are passed through to both inner transports.
    ``curl_cffi_kwargs`` overrides kwargs for the curl_cffi
    transport; ``playwright_kwargs`` overrides kwargs for the
    Playwright transport.
    """

    def __init__(
        self,
        *,
        curl_cffi_kwargs: dict[str, Any] | None = None,
        playwright_kwargs: dict[str, Any] | None = None,
        debug_dir: str = "debug_ozon_hybrid",
    ) -> None:
        self.curl_cffi_kwargs = curl_cffi_kwargs or {}
        self.playwright_kwargs = playwright_kwargs or {}
        self.debug_dir = Path(debug_dir)
        # Lazily created inner transports
        self._curl_transport = None
        self._playwright_transport = None
        # Track which transport was used for the last successful
        # fetch — useful for logging and debugging.
        self._last_transport_used: str | None = None

    # ------------------------------------------------------------------
    # Lazy inner transport creation
    # ------------------------------------------------------------------
    def _get_curl_transport(self):
        if self._curl_transport is None:
            CurlCffiTransport = _import_curl_cffi_transport()
            kwargs = {
                "debug_dir": str(self.debug_dir / "curl_cffi"),
                **self.curl_cffi_kwargs,
            }
            self._curl_transport = CurlCffiTransport(**kwargs)
        return self._curl_transport

    def _get_playwright_transport(self):
        if self._playwright_transport is None:
            BrowserJsonTransport = _import_browser_json_transport()
            kwargs = {
                "debug_dir": str(self.debug_dir / "playwright"),
                **self.playwright_kwargs,
            }
            self._playwright_transport = BrowserJsonTransport(**kwargs)
        return self._playwright_transport

    # ------------------------------------------------------------------
    # iter_ozon_reviews_json
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
        """Paginated fetch with curl_cffi→Playwright fallback.

        Strategy:

        1. Try curl_cffi for each page. If it succeeds, yield and
           continue to the next page with curl_cffi.
        2. If curl_cffi raises CloudflareChallengeError after
           exhausting retries, switch to Playwright for that page.
           Playwright can solve Cloudflare JS challenges and get
           the cf_clearance cookie.
        3. After a successful Playwright fetch, the next page is
           retried with curl_cffi (Cloudflare cookies are session-
           bound; the curl_cffi session is separate).

        ``extra_query`` appends stream-variant parameters (e.g.
        ``&sort=score_asc``) to the first page URL.
        """
        # We dispatch page-by-page. The "current transport" is the
        # one we'll try first for the next page; it can switch
        # between curl_cffi and playwright based on what works.
        current_path = self._build_initial_path(
            product_path=product_path,
            page_number=start_page,
        ) + extra_query

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
                    "Ozon (hybrid): повторный nextPage, "
                    f"остановка: {current_path}"
                )
                return

            seen_paths.add(current_path)

            payload = await self._fetch_page_with_fallback(
                product_path=product_path,
                current_path=current_path,
                processed_pages=processed_pages,
                retry_attempts=retry_attempts,
            )

            processed_pages += 1

            next_path = self.extract_next_path(payload)
            yield processed_pages, payload

            if not next_path:
                print(
                    f"Ozon (hybrid): у страницы "
                    f"{processed_pages} нет nextPage; сбор завершён"
                )
                return

            current_path = next_path

    async def _fetch_page_with_fallback(
        self,
        *,
        product_path: str,
        current_path: str,
        processed_pages: int,
        retry_attempts: int,
    ) -> dict[str, Any]:
        """Fetch one page: try curl_cffi first, fall back to
        Playwright on persistent CloudflareChallengeError.
        """
        CloudflareChallengeError = _import_cloudflare_error()
        label = (
            f"Ozon hybrid page {processed_pages + 1} "
            f"({current_path})"
        )

        # Try curl_cffi first.
        try:
            curl_transport = self._get_curl_transport()
            # Manually iterate the curl_cffi iterator for one page
            # (max_pages=1) so we can catch the challenge error.
            async for page_num, payload in curl_transport.iter_ozon_reviews_json(
                product_path=product_path,
                start_page=1,  # ignored when max_pages=1
                max_pages=1,
                retry_attempts=retry_attempts,
            ):
                self._last_transport_used = "curl_cffi"
                print(
                    f"Ozon (hybrid): {label} — OK via curl_cffi"
                )
                return payload
            # If the iterator returned nothing (shouldn't happen
            # with max_pages=1, but just in case), fall through to
            # playwright.
        except CloudflareChallengeError as exc:
            self._notify_pacer_block()
            print(
                f"Ozon (hybrid): {label} — curl_cffi исчерпал "
                f"retries с Cloudflare challenge; переключаюсь "
                f"на Playwright: {exc}"
            )
        except Exception as exc:
            print(
                f"Ozon (hybrid): {label} — curl_cffi упал с "
                f"непредвиденной ошибкой; переключаюсь на "
                f"Playwright: {exc}"
            )

        # Fall back to Playwright.
        playwright_transport = self._get_playwright_transport()
        async for page_num, payload in playwright_transport.iter_ozon_reviews_json(
            product_path=product_path,
            start_page=1,  # ignored when max_pages=1
            max_pages=1,
            retry_attempts=retry_attempts,
        ):
            self._last_transport_used = "playwright"
            print(
                f"Ozon (hybrid): {label} — OK via Playwright fallback"
            )
            return payload

        # Both failed — raise.
        raise RuntimeError(
            f"Ozon (hybrid): оба транспорта не смогли получить "
            f"страницу {processed_pages + 1} ({current_path})"
        )

    # ------------------------------------------------------------------
    # iter_ozon_reviews_by_scroll — delegate to Playwright
    # ------------------------------------------------------------------
    async def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Scroll strategy — only Playwright supports it."""
        playwright_transport = self._get_playwright_transport()
        async for batch in playwright_transport.iter_ozon_reviews_by_scroll(
            product_path=product_path,
            max_reviews=max_reviews,
        ):
            self._last_transport_used = "playwright"
            yield batch

    # ------------------------------------------------------------------
    # iter_all_ozon_reviews — pagination with curl_cffi→Playwright fallback
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
        """Yield ``(strategy, review_node)`` tuples for every unique
        review.

        For hybrid, only pagination is supported (scroll would
        require always using Playwright, which defeats the purpose
        of the curl_cffi fast path). All yielded tuples have
        ``strategy="pagination"``.
        """
        from shared.pacing import AdaptivePacer

        seen_ids: set[str] = set()

        # Adaptive inter-page pacing (see shared.pacing): shrinks
        # the delay after clean pages, backs off when curl_cffi
        # exhausts retries on a Cloudflare challenge.
        self._pacer: AdaptivePacer | None = (
            AdaptivePacer(base_delay=page_delay_seconds)
            if page_delay_seconds > 0
            else None
        )

        try:
            async for page_num, payload in (
                self.iter_ozon_reviews_json(
                    product_path=product_path,
                    start_page=pagination_start_page,
                    max_pages=pagination_max_pages,
                    retry_attempts=retry_attempts,
                )
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

                    if (
                        max_reviews is not None
                        and len(seen_ids) >= max_reviews
                    ):
                        return

                if self._pacer is not None:
                    self._pacer.record_success()
                    await self._pacer.wait()
        finally:
            self._pacer = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Close both inner transports."""
        for transport in (self._curl_transport, self._playwright_transport):
            if transport is None:
                continue
            close = getattr(transport, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                pass
        self._curl_transport = None
        self._playwright_transport = None

    @property
    def last_transport_used(self) -> str | None:
        """Which transport was used for the last successful fetch.

        Useful for debugging: if it's always ``"curl_cffi"``,
        Cloudflare is not challenging and we could disable the
        Playwright fallback for speed. If it switches to
        ``"playwright"`` often, Cloudflare is actively blocking
        curl_cffi.
        """
        return self._last_transport_used
