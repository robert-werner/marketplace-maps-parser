"""Tests for the navigation-based fetch strategy.

Previous strategy: call ``fetch()`` from the page's JS context via
``page.evaluate``. Cloudflare distinguishes these JS-level fetches
from real browser navigations and returns 403 with a challenge body
much more aggressively.

New strategy: navigate the page directly to the API URL
(``https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=...``)
using ``page.goto``. The browser treats this as a real navigation
and sends the same headers/cookies it would for a user clicking a
link. Cloudflare's bot detection typically passes these through.

These tests stub the Playwright ``page`` object to verify the new
fetch path works correctly without needing a real browser.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from infrastructure.transports.browser_json import (
    BrowserJsonTransport,
    CloudflareChallengeError,
)


# Cache the real asyncio.sleep so test monkeypatches can call it
# without infinite recursion.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(*args, **kwargs):
    """No-op replacement for ``asyncio.sleep`` so backoff doesn't wait."""
    return None


class _FakeResponse:
    """Stand-in for the Playwright Response object returned by
    ``page.goto``."""

    def __init__(
        self,
        *,
        status: int = 200,
        url: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.headers = headers or {}

    @property
    def headers_dict(self) -> dict[str, str]:
        return self.headers


class _FakePageForNavigation:
    """Stand-in page object that supports both ``page.goto`` (which
    the new navigation strategy uses) and ``page.evaluate`` (which
    we use to read the body).

    Each test configures the canned response body and metadata via
    the constructor.
    """

    def __init__(
        self,
        *,
        body: str = "",
        status: int = 200,
        url: str = "https://www.ozon.ru/api/...",
        content_type: str = "application/json",
    ) -> None:
        self._body = body
        self._status = status
        self._url = url
        self._content_type = content_type
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **kwargs) -> _FakeResponse:
        self.goto_calls.append(url)
        return _FakeResponse(
            status=self._status,
            url=self._url,
            headers={"content-type": self._content_type},
        )

    async def evaluate(self, expression: str, *args) -> Any:
        # The navigation strategy evaluates a JS expression to extract
        # the body from <pre> or <body>. We just return the canned
        # body regardless of the expression.
        return self._body

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Happy path: API returns JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_via_navigation_happy_path():
    """A 200 response with valid JSON body is parsed and returned."""
    transport = BrowserJsonTransport()

    payload_dict = {
        "nextPage": "/product/foo/reviews?page=2",
        "reviews": [{"reviewId": "r1", "rating": 5}],
    }
    import json as _json
    body_text = _json.dumps(payload_dict)

    page = _FakePageForNavigation(
        body=body_text,
        status=200,
        url="https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=...",
        content_type="application/json",
    )

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/product/foo-123/reviews?page=1",
    )

    assert result == payload_dict
    # Confirm we navigated to the API endpoint, not to the reviews page
    assert len(page.goto_calls) == 1
    assert "entrypoint-api.bx" in page.goto_calls[0]


# ---------------------------------------------------------------------------
# Cloudflare 403 with challenge body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_via_navigation_raises_cloudflare_challenge_on_403():
    """HTTP 403 with a challenge body raises CloudflareChallengeError."""
    transport = BrowserJsonTransport()

    challenge_body = (
        '{"incidentId": "fab_chlg_...", '
        '"challengeURL": "https://www.ozon.ru/challenge.html?..."}'
    )

    page = _FakePageForNavigation(
        body=challenge_body,
        status=403,
        url="https://www.ozon.ru/api/...",
        content_type="text/html",
    )

    with pytest.raises(CloudflareChallengeError) as exc_info:
        await transport._fetch_json_via_navigation(
            page=page,
            internal_path="/p",
        )

    assert exc_info.value.status == 403
    assert "incidentId" in exc_info.value.body


# ---------------------------------------------------------------------------
# Non-200, non-challenge → RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_via_navigation_raises_runtime_error_on_500():
    """HTTP 500 raises a plain RuntimeError (not CloudflareChallenge)."""
    transport = BrowserJsonTransport()

    page = _FakePageForNavigation(
        body="Internal Server Error",
        status=500,
        url="https://www.ozon.ru/api/...",
        content_type="text/html",
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await transport._fetch_json_via_navigation(
            page=page,
            internal_path="/p",
        )


# ---------------------------------------------------------------------------
# Non-JSON body when content-type says HTML
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_via_navigation_raises_on_non_json_body():
    """When the response is HTML (not JSON) and the body doesn't
    parse as JSON, raise RuntimeError."""
    transport = BrowserJsonTransport()

    page = _FakePageForNavigation(
        body="<html><body>Not JSON at all</body></html>",
        status=200,
        url="https://www.ozon.ru/api/...",
        content_type="text/html",
    )

    with pytest.raises(RuntimeError, match="не JSON"):
        await transport._fetch_json_via_navigation(
            page=page,
            internal_path="/p",
        )


# ---------------------------------------------------------------------------
# JSON body even with text/plain content-type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_via_navigation_accepts_json_body_with_text_content_type():
    """Some servers return JSON with content-type=text/plain. We
    should accept it as long as the body parses as JSON.
    """
    transport = BrowserJsonTransport()

    import json as _json
    payload = {"reviews": [{"reviewId": "r1", "rating": 5}]}
    body_text = _json.dumps(payload)

    page = _FakePageForNavigation(
        body=body_text,
        status=200,
        url="https://www.ozon.ru/api/...",
        content_type="text/plain",
    )

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/p",
    )

    assert result == payload


# ---------------------------------------------------------------------------
# Legacy fetch strategy still works (kept for comparison)
# ---------------------------------------------------------------------------


class _FakePageForLegacyFetch:
    """Stand-in page that supports the legacy ``page.evaluate(fetch())``
    pattern used by ``_fetch_json_inside_page_via_fetch``."""

    def __init__(
        self,
        *,
        body: str = "",
        status: int = 200,
        url: str = "https://www.ozon.ru/api/...",
        content_type: str = "application/json",
    ) -> None:
        self._body = body
        self._status = status
        self._url = url
        self._content_type = content_type

    async def evaluate(self, expression: str, *args) -> Any:
        # The legacy fetch strategy evaluates a JS function that
        # returns {status, url, contentType, body}. We return the
        # canned values regardless of the expression.
        return {
            "status": self._status,
            "url": self._url,
            "contentType": self._content_type,
            "body": self._body,
        }

    async def goto(self, *args, **kwargs) -> Any:
        raise AssertionError(
            "legacy fetch should NOT call page.goto"
        )


@pytest.mark.asyncio
async def test_legacy_fetch_strategy_still_works():
    """The legacy ``_fetch_json_inside_page_via_fetch`` method is
    kept for comparison / fallback. It should still parse a valid
    JSON response correctly."""
    transport = BrowserJsonTransport()

    import json as _json
    payload = {"reviews": [{"reviewId": "r1", "rating": 5}]}
    body_text = _json.dumps(payload)

    page = _FakePageForLegacyFetch(
        body=body_text,
        status=200,
        content_type="application/json",
    )

    result = await transport._fetch_json_inside_page_via_fetch(
        page=page,
        internal_path="/p",
    )

    assert result == payload


@pytest.mark.asyncio
async def test_legacy_fetch_strategy_raises_cloudflare_challenge():
    """Legacy strategy also detects Cloudflare 403 challenges."""
    transport = BrowserJsonTransport()

    challenge_body = (
        '{"incidentId": "fab_chlg_...", '
        '"challengeURL": "https://www.ozon.ru/challenge.html?..."}'
    )

    page = _FakePageForLegacyFetch(
        body=challenge_body,
        status=403,
        content_type="text/html",
    )

    with pytest.raises(CloudflareChallengeError):
        await transport._fetch_json_inside_page_via_fetch(
            page=page,
            internal_path="/p",
        )


# ---------------------------------------------------------------------------
# _fetch_json_inside_page dispatches to navigation by default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_json_inside_page_uses_navigation_by_default(monkeypatch):
    """``_fetch_json_inside_page`` should call ``_fetch_json_via_navigation``
    by default (when ``fetch_strategy="navigation"``).
    """
    transport = BrowserJsonTransport(fetch_strategy="navigation")

    called = {"navigation": 0, "fetch": 0}

    async def fake_navigation(*, page, internal_path):
        called["navigation"] += 1
        return {"ok": True, "via": "navigation"}

    async def fake_legacy(*, page, internal_path):
        called["fetch"] += 1
        return {"ok": True, "via": "fetch"}

    monkeypatch.setattr(
        transport, "_fetch_json_via_navigation", fake_navigation,
    )
    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page_via_fetch",
        fake_legacy,
    )

    result = await transport._fetch_json_inside_page(
        page=None,  # type: ignore
        internal_path="/p",
    )

    assert result == {"ok": True, "via": "navigation"}
    assert called["navigation"] == 1
    assert called["fetch"] == 0


@pytest.mark.asyncio
async def test_fetch_json_inside_page_uses_legacy_fetch_when_configured(monkeypatch):
    """When ``fetch_strategy="fetch"``, ``_fetch_json_inside_page``
    should call the legacy ``_fetch_json_inside_page_via_fetch``.
    """
    transport = BrowserJsonTransport(fetch_strategy="fetch")

    called = {"navigation": 0, "fetch": 0}

    async def fake_navigation(*, page, internal_path):
        called["navigation"] += 1
        return {"ok": True, "via": "navigation"}

    async def fake_legacy(*, page, internal_path):
        called["fetch"] += 1
        return {"ok": True, "via": "fetch"}

    monkeypatch.setattr(
        transport, "_fetch_json_via_navigation", fake_navigation,
    )
    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page_via_fetch",
        fake_legacy,
    )

    result = await transport._fetch_json_inside_page(
        page=None,  # type: ignore
        internal_path="/p",
    )

    assert result == {"ok": True, "via": "fetch"}
    assert called["navigation"] == 0
    assert called["fetch"] == 1


def test_transport_rejects_unknown_fetch_strategy():
    """Unknown fetch_strategy raises ValueError at construction."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="Unknown fetch_strategy"):
        BrowserJsonTransport(fetch_strategy="bogus")
