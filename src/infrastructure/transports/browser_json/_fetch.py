"""Mixin."""
from __future__ import annotations
import asyncio
from typing import Any
from infrastructure.transports.browser_json._errors import CloudflareChallengeError
import infrastructure.transports.browser_json as _mod


class FetchMixin:
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
            retry_on=_mod._retryable_errors(),
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
