"""Bounded JSON requests using the running browser's network stack."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from urllib.parse import urljoin, urlsplit


class BrowserApiError(RuntimeError):
    """HTTP, blocked, timed-out or malformed browser API response."""


_FETCH_JSON = """
async ({url, timeoutMs, method, body, headers, credentials}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            method, body: body || undefined, headers, credentials,
            signal: controller.signal,
        });
        const text = await response.text();
        let payload = null;
        try { payload = JSON.parse(text); } catch {}
        return {status: response.status, payload};
    } finally { clearTimeout(timer); }
}
"""


async def fetch_json(
    page: Any, url: str, *, timeout_ms: int = 30_000,
    method: str = "GET", body: str | None = None,
    headers: dict[str, str] | None = None, credentials: str = "include",
) -> dict[str, Any]:
    """No HTTP client, hard-coded auth token, response dump or URL logging."""
    try:
        result = await page.evaluate(_FETCH_JSON, {
            "url": url, "timeoutMs": timeout_ms, "method": method,
            "body": body, "credentials": credentials,
            "headers": {"Accept": "application/json", **(headers or {})},
        })
    except Exception as exc:
        raise BrowserApiError(
            f"Browser API request failed ({type(exc).__name__})",
        ) from exc
    if not isinstance(result, dict):
        raise BrowserApiError("Invalid browser API response envelope")
    status = result.get("status", 0)
    if not isinstance(status, int) or not 200 <= status < 300:
        raise BrowserApiError(f"Browser API HTTP {status}")
    payload = result.get("payload")
    if not isinstance(payload, dict):
        raise BrowserApiError("Browser API returned non-object JSON")
    return payload


def same_endpoint_url(base: str, candidate: str) -> str:
    """Follow a server cursor only on the *same* HTTPS endpoint.

    Never forward a page-supplied token/cursor to a different host or account.
    """
    url = urljoin(base, candidate)
    expected, actual = urlsplit(base), urlsplit(url)
    if (
        expected.scheme != "https"
        or actual.scheme != "https"
        or actual.netloc != expected.netloc
        or actual.path != expected.path
        or actual.username or actual.password
    ):
        raise BrowserApiError("API cursor points outside the review endpoint")
    return url


class ResponseCapture(AbstractContextManager["ResponseCapture"]):
    """Keep only a few relevant responses; no tasks or unbounded body cache."""

    def __init__(
        self, page: Any, match: Callable[[str], bool], *, capacity: int = 8,
    ) -> None:
        self.page = page
        self.match = match
        self.responses: deque[Any] = deque(maxlen=capacity)
        self.attached = False

    def _receive(self, response: Any) -> None:
        if self.match(response.url):
            self.responses.append(response)

    def __enter__(self) -> ResponseCapture:
        if hasattr(self.page, "on"):
            self.page.on("response", self._receive)
            self.attached = True
        return self

    def __exit__(self, *args: Any) -> None:
        if self.attached:
            self.page.remove_listener("response", self._receive)
        self.responses.clear()
