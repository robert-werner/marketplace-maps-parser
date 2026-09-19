# src/infrastructure/transports/wb_browser.py
"""Wildberries HTTP-API transport backed by a browser session.

WB blocks every non-browser client cold: the card/feedbacks APIs
answer ``403 Forbidden`` and even ``https://www.wildberries.ru/``
returns ``498`` for plain httpx AND curl_cffi with chrome TLS
impersonation (measured 2026-09-19). A real browser session passes
— so the transport loads the product page once (cookies + a
trusted fingerprint) and serves the adapter's ``get_json`` calls
via in-page ``fetch()`` (same-origin cookies, the site's own CORS).

The class matches the adapter's
:class:`WildberriesHttpTransport` Protocol (``get_json``), so
``WildberriesAdapter`` works unchanged.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
from infrastructure.transports.http import HttpStatusError

_FETCH_JSON_JS = """
async (url) => {
    const resp = await fetch(url, {
        headers: {'Accept': 'application/json'},
    });
    const text = await resp.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) {}
    return {status: resp.status, payload};
}
"""


class WildberriesBrowserTransport:
    """One page load per product; JSON APIs via in-page fetch."""

    def __init__(
        self,
        *,
        product_url: str,
        timeout_ms: int = 90_000,
        settle_ms: int = 4_000,
        proxy: dict[str, str] | None = None,
        humanize: bool = True,
    ) -> None:
        self.product_url = product_url
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.proxy = proxy
        self.humanize = humanize
        self._page: Any = None
        self._browser_ctx: Any = None

    async def __aenter__(self) -> WildberriesBrowserTransport:
        browser_cls = import_invisible_playwright()
        self._browser_ctx = browser_cls(
            proxy=self.proxy,
            seed=None,
            humanize=self.humanize,
        )
        browser = await self._browser_ctx.__aenter__()
        self._page = await browser.new_page()
        await self._page.goto(
            self.product_url,
            timeout=self.timeout_ms,
        )
        await self._page.wait_for_timeout(self.settle_ms)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._browser_ctx is not None:
            try:
                await self._browser_ctx.__aexit__(exc_type, exc, tb)
            except Exception:
                pass
        self._page = None
        self._browser_ctx = None

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch ``url`` from the page context → parsed JSON dict.

        ``headers`` is accepted for Protocol compatibility and
        ignored: the in-page fetch already carries the site's
        cookies and a browser fingerprint."""
        full_url = url
        if params:
            full_url = f"{url}?{urlencode(params)}"
        out = await self._page.evaluate(_FETCH_JSON_JS, full_url)
        status = out.get("status") if isinstance(out, dict) else None
        if isinstance(status, int) and status >= 400:
            raise HttpStatusError(status_code=status, url=full_url)
        payload = (
            out.get("payload") if isinstance(out, dict) else None
        )
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Ожидался JSON dict, получен "
                f"status={status}: {full_url}",
            )
        return payload


__all__ = ["WildberriesBrowserTransport"]
