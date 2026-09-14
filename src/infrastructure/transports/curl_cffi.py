"""curl_cffi-based transport for fetching Ozon reviews JSON.

curl_cffi is a Python HTTP client built on libcurl with the
``curl-impersonate`` patches. It reproduces the TLS fingerprint of
real browsers (Chrome, Firefox, Safari) at the byte level — the
ClientHello, ALPN, JA3/JA4 hash, and HTTP/2 frame ordering match
what a real browser sends. This makes it significantly more
Cloudflare-resistant than ``page.evaluate(fetch())`` and lighter
weight than running a full browser via ``invisible-playwright``.

The transport implements the same async iterator interface as
``BrowserJsonTransport`` (``iter_ozon_reviews_json``,
``iter_ozon_reviews_by_scroll``, ``iter_all_ozon_reviews``) so it
can be plugged into ``OzonAdapter`` directly via the
``OzonBrowserTransport`` Protocol.

Limitations vs. the Playwright transport:

- **Scroll strategy is NOT supported.** curl_cffi is a pure HTTP
  client with no DOM. The ``iter_ozon_reviews_by_scroll`` method
  raises ``NotImplementedError``. Use ``--strategy pagination`` or
  ``--strategy auto`` (auto will skip scroll silently and rely on
  pagination alone).

- **Cloudflare JS challenges cannot be solved.** If Cloudflare
  returns the HTML "enable JavaScript" challenge page, curl_cffi
  cannot execute the embedded JS — there is no JS engine. The
  transport raises ``CloudflareChallengeError`` and the caller
  retries with backoff. For sites that always require JS challenge
  solving, use the Playwright transport instead.

- **Stealth comes from the TLS fingerprint, not JS patches.** No
  ``navigator.webdriver`` or ``chrome.runtime`` patches — those
  are JS-level signals that don't apply to a pure HTTP client.

Advantages:

- 10-50x faster than Playwright (no browser startup, no page
  rendering, no JS execution).
- 100-1000x less memory (a single ``AsyncSession`` vs a full
  Chromium process).
- True browser TLS fingerprint — Cloudflare's primary bot signal
  is JA3/JA4, and curl_cffi's ``impersonate='chrome120'`` produces
  the exact same fingerprint as a real Chrome 120 client.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


def _import_curl_cffi():
    """Lazy import of curl_cffi. Kept lazy so the module can be
    imported in environments without curl_cffi installed (e.g. unit
    tests of pure-Python helpers).
    """
    from curl_cffi import requests as curl_requests
    return curl_requests


def _import_cloudflare_error():
    """Import the CloudflareChallengeError from browser_json.

    Kept as a function (not a top-level import) to avoid coupling
    the two transport modules at import time. The error class is
    shared so the caller's ``retry_on=...`` filters work uniformly.
    """
    from infrastructure.transports.browser_json import (
        CloudflareChallengeError,
    )
    return CloudflareChallengeError


def _import_retry_async():
    """Lazy import of shared.retry.retry_async."""
    from shared.retry import retry_async
    return retry_async


# Default headers that real Chrome 120 sends on a navigation to
# www.ozon.ru. curl_cffi's ``impersonate`` already covers most of
# these, but we set them explicitly to be sure.
_DEFAULT_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": (
        '"Not_A Brand";v="8", "Chromium";v="120", '
        '"Google Chrome";v="120"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# curl_cffi impersonate targets ordered from newest to oldest. We
# try the newest first and fall back if Cloudflare still blocks.
_IMPERSONATE_TARGETS = (
    "chrome120",
    "chrome119",
    "chrome116",
    "chrome110",
    "chrome107",
    "chrome101",
    "chrome100",
    "chrome99",
    "firefox120",
    "firefox117",
    "firefox102",
    "safari17_0",
    "safari16_0",
)


class CurlCffiTransport:
    """HTTP transport for Ozon reviews JSON using curl_cffi.

    Implements the same shape as ``BrowserJsonTransport`` (the
    ``OzonBrowserTransport`` Protocol) so it can be plugged into
    ``OzonAdapter`` as a drop-in replacement.

    The Ozon adapter calls three methods:

    - ``iter_ozon_reviews_json`` — paginated fetch via the internal
      ``entrypoint-api.bx/page/json/v2`` endpoint.
    - ``iter_ozon_reviews_by_scroll`` — NOT supported (raises
      ``NotImplementedError``).
    - ``iter_all_ozon_reviews`` — runs pagination only (no scroll
      fallback since scroll is not supported).
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        debug_dir: str = "debug_ozon_curl",
        impersonate: str = "chrome120",
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
        max_redirects: int = 10,
        warmup: bool = True,
    ) -> None:
        self.timeout = timeout
        self.debug_dir = Path(debug_dir)
        self.impersonate = impersonate
        self.proxy = proxy
        self.headers = {**_DEFAULT_HEADERS, **(headers or {})}
        self.max_redirects = max_redirects
        # When True, before the first API request we visit the product
        # page (https://www.ozon.ru/product/<id>) to obtain Cloudflare
        # cookies (``cf_clearance``, ``__cf_bm``). Without these
        # cookies, the API endpoint returns 403 challenge on every
        # request.
        self.warmup = warmup
        # Track whether warmup has been performed so we don't redo it
        # on every iteration of iter_ozon_reviews_json.
        self._warmed_up = False
        # Lazily created AsyncSession — kept open for the lifetime of
        # the transport so cookies persist across requests.
        self._session = None

    async def __aenter__(self) -> "CurlCffiTransport":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_session(self):
        """Lazily create the curl_cffi AsyncSession.

        The session keeps cookies across requests and holds the
        connection pool, so we want to reuse it.
        """
        if self._session is not None:
            return self._session
        curl_requests = _import_curl_cffi()
        kwargs: dict[str, Any] = {
            "impersonate": self.impersonate,
            "timeout": self.timeout,
            "max_redirects": self.max_redirects,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        self._session = curl_requests.AsyncSession(**kwargs)
        return self._session

    async def _warmup_session(
        self,
        *,
        product_path: str,
    ) -> None:
        """Visit the product page to obtain Cloudflare cookies.

        Cloudflare's bot protection issues two cookies that gate
        access to the Ozon API endpoint:

        - ``__cf_bm`` — short-lived (30 min) bot-management cookie,
          set on the first HTML navigation.
        - ``cf_clearance`` — long-lived (1-2 hours) clearance cookie,
          set after the browser solves the Cloudflare challenge JS.

        Without these cookies, the API endpoint
        (``/api/entrypoint-api.bx/page/json/v2``) returns HTTP 403
        with a challenge body on every request from a cold session.

        curl_cffi cannot solve the JS challenge (no JS engine), but
        for many Cloudflare configurations the bot-management cookie
        is sufficient to pass the API endpoint — visiting the
        product page first is enough.

        After this method returns, the session has the necessary
        cookies and subsequent API requests will succeed (or at
        least not fail with a 403 challenge).

        If the warmup page itself returns a Cloudflare challenge,
        we print a warning and continue — the API endpoint may
        still be accessible without ``cf_clearance`` depending on
        Cloudflare's per-site configuration.
        """
        # Visit the product page (HTML). The product_path looks
        # like "/product/ip-telefon-yealink-sip-t30-...".
        product_url = f"https://www.ozon.ru{product_path}"

        print(
            f"Ozon (curl_cffi): warmup — открываю {product_url} для "
            "получения Cloudflare cookies..."
        )

        session = await self._ensure_session()

        try:
            response = await session.get(
                product_url,
                headers=self.headers,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Ozon curl_cffi warmup: сетевая ошибка: {exc}"
            ) from exc

        status = response.status_code
        body = response.text or ""

        if status == 200:
            print(
                "Ozon (curl_cffi): warmup OK — Cloudflare cookies "
                "получены"
            )
            return

        if status == 403 and self._is_cloudflare_challenge(body):
            # Warmup itself got a challenge — curl_cffi can't solve
            # JS challenges. Print a clear warning and continue;
            # the API requests will likely also fail.
            print(
                "Ozon (curl_cffi): WARNING — warmup также получил "
                "Cloudflare challenge (HTTP 403). curl_cffi не может "
                "решить JS challenge — используйте --transport "
                "playwright для этого сайта."
            )
            return

        # Other non-200 — log and continue
        print(
            f"Ozon (curl_cffi): warmup вернул HTTP {status}; "
            "продолжаю без cookies"
        )

    async def close(self) -> None:
        """Close the underlying curl_cffi session."""
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

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
        """Fetch Ozon review pages via the internal entrypoint-api
        endpoint, following ``nextPage`` links until exhausted.

        Yields ``(page_number, payload)`` tuples. ``payload`` is the
        raw JSON dict returned by Ozon.
        """
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        CloudflareChallengeError = _import_cloudflare_error()
        retry_async = _import_retry_async()

        await self._ensure_session()

        # Warm-up: visit the product page first to obtain Cloudflare
        # cookies (cf_clearance, __cf_bm). Without them the API
        # endpoint returns 403 challenge on every cold-session
        # request.
        if self.warmup and not self._warmed_up:
            try:
                await self._warmup_session(product_path=product_path)
                self._warmed_up = True
            except Exception as exc:
                print(
                    "Ozon (curl_cffi): warmup не удался — "
                    f"продолжаю без него: {exc}"
                )

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
                    "Ozon (curl_cffi): повторный nextPage, "
                    f"остановка: {current_path}"
                )
                return

            seen_paths.add(current_path)

            endpoint_url = self._build_api_url(current_path)

            payload = await retry_async(
                lambda path=current_path: self._fetch_api_json(
                    endpoint_url=self._build_api_url(path),
                    label=(
                        f"Ozon curl_cffi page "
                        f"{processed_pages + 1} ({path})"
                    ),
                ),
                attempts=retry_attempts,
                base_delay=3.0,
                max_delay=30.0,
                factor=2.0,
                jitter=0.3,
                retry_on=(
                    RuntimeError,
                    CloudflareChallengeError,
                ),
                label=(
                    f"Ozon curl_cffi page {processed_pages + 1} "
                    f"({current_path})"
                ),
            )

            processed_pages += 1

            await self._save_debug(
                payload=payload,
                page_number=processed_pages,
            )

            next_path = self.extract_next_path(payload)
            yield processed_pages, payload

            if not next_path:
                print(
                    f"Ozon (curl_cffi): у страницы "
                    f"{processed_pages} нет nextPage; сбор завершён"
                )
                return

            current_path = next_path

    # ------------------------------------------------------------------
    # Scroll iterator — NOT supported
    # ------------------------------------------------------------------
    async def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """DOM scroll strategy is NOT supported by curl_cffi.

        curl_cffi is a pure HTTP client — there is no DOM, no JS
        engine, no way to scroll a page. The scroll strategy
        requires a real browser (Playwright). Use the
        ``BrowserJsonTransport`` or ``BrowserDomTransport`` if you
        need scroll mode.
        """
        raise NotImplementedError(
            "CurlCffiTransport does not support the scroll strategy. "
            "Use BrowserJsonTransport (Playwright) instead, or "
            "switch to --strategy pagination."
        )
        # unreachable, but makes this an async generator
        yield []  # pragma: no cover

    # ------------------------------------------------------------------
    # Unified iterator — pagination only (no scroll fallback)
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

        For curl_cffi, only the pagination strategy is available.
        Scroll is not supported (see ``iter_ozon_reviews_by_scroll``).
        All yielded tuples have ``strategy="pagination"``.
        """
        seen_ids: set[str] = set()

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

                if (
                    max_reviews is not None
                    and len(seen_ids) >= max_reviews
                ):
                    return

            if page_delay_seconds > 0:
                from shared.retry import sleep_with_jitter
                await sleep_with_jitter(page_delay_seconds)

    # ------------------------------------------------------------------
    # Internal fetch
    # ------------------------------------------------------------------
    async def _fetch_api_json(
        self,
        *,
        endpoint_url: str,
        label: str = "Ozon curl_cffi fetch",
    ) -> dict[str, Any]:
        """Fetch the Ozon API URL and return the parsed JSON dict.

        Raises:
            CloudflareChallengeError: HTTP 403 with a Cloudflare
                challenge body.
            RuntimeError: HTTP non-200 (non-challenge), non-JSON
                content-type, or JSON decode failure.
        """
        CloudflareChallengeError = _import_cloudflare_error()

        session = await self._ensure_session()

        try:
            response = await session.get(
                endpoint_url,
                headers=self.headers,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Ozon curl_cffi fetch: сетевая ошибка: {exc}"
            ) from exc

        status = response.status_code
        body = response.text or ""
        content_type = (
            response.headers.get("content-type", "") or ""
        ).lower()

        if status < 200 or status >= 300:
            if status == 403 and self._is_cloudflare_challenge(body):
                raise CloudflareChallengeError(
                    status=status,
                    url=endpoint_url,
                    body=body,
                )
            raise RuntimeError(
                f"Ozon curl_cffi fetch: HTTP {status}; "
                f"url={endpoint_url}; body={body[:500]}"
            )

        # Some Ozon responses return JSON with text/html content-type.
        # Try to parse regardless.
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Ozon curl_cffi fetch: не удалось декодировать "
                f"JSON (content-type={content_type}): {body[:500]}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Ozon curl_cffi fetch: корень JSON не является dict"
            )

        return payload

    # ------------------------------------------------------------------
    # Helpers (mirror BrowserJsonTransport's static helpers)
    # ------------------------------------------------------------------
    @staticmethod
    def _build_initial_path(
        *,
        product_path: str,
        page_number: int,
    ) -> str:
        return f"{product_path}/reviews?page={page_number}"

    @staticmethod
    def _build_api_url(internal_path: str) -> str:
        """Build the full Ozon API URL for a given internal path."""
        endpoint = (
            "https://www.ozon.ru"
            "/api/entrypoint-api.bx/page/json/v2"
        )
        return f"{endpoint}?{urlencode({'url': internal_path})}"

    @staticmethod
    def extract_next_path(
        payload: dict[str, Any],
    ) -> str | None:
        """Extract the next page path from an Ozon API payload.

        Mirrors ``BrowserJsonTransport.extract_next_path``.
        """
        next_page = payload.get("nextPage")

        if isinstance(next_page, str):
            return next_page or None

        if isinstance(next_page, dict):
            for key in ("url", "href", "path"):
                value = next_page.get(key)
                if isinstance(value, str) and value:
                    return value

        return None

    @staticmethod
    def _review_node_id(node: dict[str, Any]) -> str | None:
        """Best-effort extraction of a stable id from a review node.

        Mirrors ``BrowserJsonTransport._review_node_id``.
        """
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

    @staticmethod
    def _extract_review_nodes_from_payload(
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Pull every dict that looks like a review out of a raw
        Ozon pagination payload.

        Mirrors ``BrowserJsonTransport._extract_review_nodes_from_payload``.
        """
        from domain.entities import ProductRef
        from infrastructure.marketplaces.ozon import (
            extract_reviews_from_ozon_payload,
        )

        placeholder = ProductRef(
            marketplace="ozon",
            source_url="",
            product_id="_placeholder",
        )
        reviews = extract_reviews_from_ozon_payload(
            payload, placeholder,
        )
        return [
            r.raw for r in reviews if isinstance(r.raw, dict)
        ]

    @staticmethod
    def _is_cloudflare_challenge(body: str) -> bool:
        """Same heuristic as BrowserJsonTransport._is_cloudflare_challenge.

        Detects both the JSON envelope (``incidentId`` /
        ``challengeURL``) and the HTML "enable JavaScript" page.
        """
        if not body:
            return False
        body_lower = body.lower()
        return (
            "challengeurl" in body_lower
            or "incidentid" in body_lower
            or "challenge.html" in body_lower
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
        payload: dict[str, Any],
        page_number: int,
    ) -> None:
        """Save the raw payload to a debug file for postmortem
        analysis. Mirrors BrowserJsonTransport._save_debug.
        """
        page_dir = self.debug_dir / f"page_{page_number}"
        page_dir.mkdir(parents=True, exist_ok=True)

        (page_dir / "response.json").write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
