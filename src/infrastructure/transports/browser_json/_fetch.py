"""Mixin."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakKeyDictionary

import infrastructure.transports.browser_json as _mod
from infrastructure.transports.base import OzonTransportMixin
from infrastructure.transports.browser_json._errors import (
    CloudflareChallengeError,
)


class FetchMixin(OzonTransportMixin):
    block_assets: bool
    fetch_strategy: str
    timeout_ms: int
    settle_ms: int
    _page_fetch_modes: WeakKeyDictionary[Any, str]

    # ------------------------------------------------------------------
    # Per-page speed helpers
    # ------------------------------------------------------------------
    #
    # The JSON fetch needs no images/fonts/media. Route by extension,
    # not "**/*", to keep document/script/XHR requests out of Python.
    _BLOCKED_RESOURCE_TYPES = frozenset(
        {"image", "font", "media"}
    )

    _ASSET_ROUTE_PATTERNS = (
        "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.webp",
        "**/*.gif", "**/*.avif", "**/*.woff", "**/*.woff2",
        "**/*.ttf", "**/*.mp4",
    )

    async def _install_resource_blocker(self, page: Any) -> None:
        if not self.block_assets:
            return

        async def _route(route: Any) -> None:
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

    async def _fetch_json_inside_page(
        self,
        *,
        page: Any,
        internal_path: str,
    ) -> dict[str, Any]:
        """Prefer in-page fetch, learning navigation fallback per tab.

        Never use a separate HTTP client: both paths keep the browser's
        cookies, network stack and proxy. A tab showing Firefox's JSON
        viewer cannot keep fetching as an Ozon document, so once we fall
        back, that tab stays on the navigation path.
        """
        mode = (
            self._page_fetch_modes.get(page, "auto")
            if self.fetch_strategy == "auto" else self.fetch_strategy
        )
        if mode in ("auto", "fetch"):
            try:
                return await self._fetch_json_inside_page_via_fetch(
                    page=page,
                    internal_path=internal_path,
                )
            except _mod._retryable_errors():
                if mode == "fetch":
                    raise
                self._page_fetch_modes[page] = "navigation"
                print("Ozon: fetch недоступен; пробую API-навигацию")
        return await self._fetch_json_via_navigation(
            page=page,
            internal_path=internal_path,
        )

    async def _wait_for_reviews_ready(self, page: Any) -> None:
        """Wait for review content, retrying only context-loss races.

        Ozon can return DOMContentLoaded for an interstitial and then
        replace that document. A fixed sleep both wastes warm-session
        time and sometimes sends the first API call from that interstitial.
        """
        wait = getattr(page, "wait_for_function", None)
        if wait is None:  # Compatibility with minimal browser wrappers.
            if self.settle_ms > 0:
                await page.wait_for_timeout(self.settle_ms)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.timeout_ms / 1000, 30.0)
        while True:
            remaining = max(1, int((deadline - loop.time()) * 1000))
            try:
                handle = await wait(
                    """() => !!document.querySelector(
                        '[data-review-uuid],'
                        + '[data-widget="webListReviews"],'
                        + '[data-widget="webReviewProductScore"]'
                    )""",
                    timeout=remaining,
                )
                await handle.dispose()
                return
            except _mod._retryable_errors() as exc:
                message = str(exc).lower()
                transient = any(part in message for part in (
                    "execution context", "operation was aborted",
                    "cannot find context", "context was destroyed",
                ))
                if not transient or loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.1)

    async def _fetch_json_via_navigation(
        self,
        *,
        page: Any,
        internal_path: str,
    ) -> dict[str, Any]:
        """Navigate to the API, reading raw JSON rather than the viewer UI.

        Navigations can also receive interstitials; allow the browser
        a bounded time to finish a redirect, then surface any failure.
        """
        endpoint_url = self._build_api_url(internal_path)

        # Keep browser cookies and the selected session proxy.
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
        if self._is_cloudflare_challenge(body):
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

        if self._is_cloudflare_challenge(body):
            self._notify_pacer_block(page)
            raise CloudflareChallengeError(
                status=status, url=response_url, body=body,
            )

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
            raise RuntimeError(
                "Ozon navigation fetch завершился ошибкой: "
                f"HTTP {status}; url={response_url}; "
                f"response_chars={len(body)}"
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
                f"response_chars={len(body)}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Не удалось декодировать JSON Ozon ({len(body)} chars)"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Корень JSON Ozon не является dict"
            )

        return payload

    async def _goto_and_read_body(
        self,
        *,
        page: Any,
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
            # The response body is read from the Response object. Waiting
            # for DOMContentLoaded only waits for the JSON viewer UI.
            wait_until="commit",
            timeout=self.timeout_ms,
        )

        body = ""
        if response is not None:
            try:
                body = await response.text() or ""
            except Exception:
                body = ""

        if not body:
            # A commit is not a loaded JSON document. In particular the
            # JSON viewer's text node can still be empty/partially filled.
            deadline = asyncio.get_running_loop().time() + min(
                self.timeout_ms / 1000, 5.0,
            )
            while True:
                try:
                    body = await self._read_page_body(page)
                except RuntimeError:
                    body = ""
                if body and self._body_is_ready(body):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.1)

        return response, body

    @staticmethod
    def _body_is_ready(body: str) -> bool:
        if not body.lstrip().startswith(("{", "[")):
            return bool(body)
        try:
            json.loads(body)
        except ValueError:
            return False
        return True

    async def _read_page_body(self, page: Any) -> str:
        """Read the rendered page's body text.

        Ozon's API returns raw JSON which browsers render inside a
        ``<pre>`` element. We prefer ``<pre>`` over ``document.body``
        because some browsers add whitespace/annotations to the
        body text.
        """
        try:
            body = await page.evaluate(
                """
                () => {
                    // Firefox stores the original response in a Text node;
                    // body.textContent is only the JSON viewer's tree UI.
                    const raw = window.JSONView?.json?.textContent;
                    if (typeof raw === "string") return raw;
                    const json = document.getElementById("json");
                    if (json) return json.textContent || "";
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
            if not isinstance(body, str):
                raise TypeError("Expected a string body from the browser")
            return body
        except Exception as exc:
            raise RuntimeError(
                "Ozon navigation fetch: не удалось прочитать тело "
                f"ответа: {exc}"
            ) from exc

    async def _wait_for_challenge_completion(
        self,
        *,
        page: Any,
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
            if current_body and not self._is_cloudflare_challenge(
                current_body,
            ):
                # The body changed. If it looks like JSON, we're done.
                stripped = current_body.lstrip()
                if stripped.startswith(("{", "[")) and self._body_is_ready(
                    current_body,
                ):
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
                if stripped.startswith(("{", "[")):
                    continue  # JSON is still arriving in chunks.
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
        page: Any,
        internal_path: str,
    ) -> dict[str, Any]:
        """Read the API in the warm Ozon document, with a bounded timeout."""
        endpoint_url = self._build_api_url(internal_path)

        result = await page.evaluate(
            """
            async ({endpointUrl, timeoutMs}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const response = await fetch(endpointUrl, {
                        method: "GET",
                        credentials: "include",
                        signal: controller.signal,
                        headers: {"Accept": "application/json"}
                    });
                    return {
                        status: response.status,
                        url: response.url,
                        contentType:
                            response.headers.get("content-type") || "",
                        body: await response.text()
                    };
                } finally {
                    clearTimeout(timer);
                }
            }
            """,
            {"endpointUrl": endpoint_url, "timeoutMs": self.timeout_ms},
        )

        status = result["status"]
        response_url = result["url"]
        content_type = result["contentType"].lower()
        body = result["body"]

        if self._is_cloudflare_challenge(body):
            self._notify_pacer_block(page)
            raise CloudflareChallengeError(
                status=status, url=response_url, body=body,
            )

        if status < 200 or status >= 300:
            raise RuntimeError(
                "Ozon browser fetch завершился ошибкой: "
                f"HTTP {status}; url={response_url}; "
                f"response_chars={len(body)}"
            )

        if "json" not in content_type:
            raise RuntimeError(
                "Ozon browser fetch вернул не JSON: "
                f"content-type={content_type}; "
                f"response_chars={len(body)}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Не удалось декодировать JSON Ozon ({len(body)} chars)"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Корень JSON Ozon не является dict"
            )

        return payload

    async def _goto_with_retry(
        self,
        *,
        page_factory: Callable[[], Awaitable[Any]],
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
                wait_until=(
                    "domcontentloaded"
                    if self.fetch_strategy in ("auto", "fetch")
                    else "commit"
                ),
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
            retry_on=_mod._retryable_errors(),
            label=label,
        )

    async def _fetch_json_with_retry(
        self,
        *,
        page: Any,
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
        # retry_on=_mod._retryable_errors() catches it automatically. We
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
            retry_on=_mod._retryable_errors(),
            label=label,
        )
