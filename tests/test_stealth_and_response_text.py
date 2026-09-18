"""Tests for stealth init script and raw response body extraction.

Two production concerns addressed in this commit:

1. ``response.text()`` is now used as the primary source for the
   response body. Previously we read ``document.body.textContent``,
   which returns the *rendered* body — when the browser's built-in
   JSON viewer renders the API response (Firefox shows a tree UI
   for ``application/json`` URLs), the rendered text contains
   viewer UI strings (``JSONRaw DataHeadersSaveCopyCollapse All…``)
   and is not parseable JSON.

2. Stealth mode (``stealth=True`` by default) applies an init
   script to every fresh page that patches ``navigator.webdriver``,
   ``chrome.runtime``, ``Notification.permission``, plugins,
   mimeTypes, languages, and other signals Cloudflare uses to
   detect automated browsers.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from infrastructure.transports.browser_json.transport import (
    _STEALTH_INIT_SCRIPT,
    BrowserJsonTransport,
)

# Cache the real asyncio.sleep so test monkeypatches can call it
# without infinite recursion.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(*args, **kwargs):
    return None


# ---------------------------------------------------------------------------
# Stealth init script
# ---------------------------------------------------------------------------


def test_stealth_init_script_is_non_empty_string():
    """The stealth init script must be a non-empty JS string."""
    assert isinstance(_STEALTH_INIT_SCRIPT, str)
    assert len(_STEALTH_INIT_SCRIPT) > 100


def test_stealth_init_script_patches_navigator_webdriver():
    """The init script must patch ``navigator.webdriver`` to
    undefined — Cloudflare's primary bot-detection signal."""
    assert "navigator" in _STEALTH_INIT_SCRIPT
    assert "webdriver" in _STEALTH_INIT_SCRIPT
    assert "undefined" in _STEALTH_INIT_SCRIPT


def test_stealth_init_script_adds_chrome_runtime():
    """The init script must add a fake ``window.chrome.runtime``
    object — real Chrome browsers expose it."""
    assert "window.chrome" in _STEALTH_INIT_SCRIPT
    assert "runtime" in _STEALTH_INIT_SCRIPT


def test_stealth_init_script_patches_notification_permission():
    """The init script must patch ``Notification.permission`` to
    'default' — headless browsers report 'denied'."""
    assert "Notification" in _STEALTH_INIT_SCRIPT
    assert "permission" in _STEALTH_INIT_SCRIPT
    assert "default" in _STEALTH_INIT_SCRIPT


def test_stealth_init_script_adds_plugins():
    """The init script must add fake plugins (PDF viewer) — real
    browsers have at least one."""
    assert "plugins" in _STEALTH_INIT_SCRIPT
    assert "PDF" in _STEALTH_INIT_SCRIPT


def test_stealth_init_script_patches_languages():
    """The init script must set navigator.languages to a realistic
    list (ru, ru-RU, en-US, en)."""
    assert "languages" in _STEALTH_INIT_SCRIPT
    assert "ru" in _STEALTH_INIT_SCRIPT


def test_stealth_init_script_patches_outer_dimensions():
    """The init script must patch window.outerWidth / outerHeight
    to non-zero — headless browsers report 0."""
    assert "outerWidth" in _STEALTH_INIT_SCRIPT
    assert "outerHeight" in _STEALTH_INIT_SCRIPT


# ---------------------------------------------------------------------------
# Transport stealth configuration
# ---------------------------------------------------------------------------


def test_transport_stealth_enabled_by_default():
    """Stealth mode is on by default."""
    transport = BrowserJsonTransport()
    assert transport.stealth is True


def test_transport_stealth_can_be_disabled():
    """Stealth can be disabled via constructor arg."""
    transport = BrowserJsonTransport(stealth=False)
    assert transport.stealth is False


# ---------------------------------------------------------------------------
# page.add_init_script is called for new pages when stealth is enabled
# ---------------------------------------------------------------------------


class _FakePageWithInitScript:
    """Stand-in page that records whether add_init_script was called."""

    def __init__(self) -> None:
        self.init_scripts_added: list[str] = []
        self.goto_calls: list[str] = []
        self.closed = False

    async def add_init_script(self, script: str) -> None:
        self.init_scripts_added.append(script)

    async def goto(self, url: str, **kwargs) -> Any:
        self.goto_calls.append(url)
        return None

    async def evaluate(self, expression: str, *args) -> Any:
        return ""

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserWithNewPage:
    """Stand-in browser that returns fresh fake pages."""

    def __init__(self) -> None:
        self.pages_created = 0
        self.pages: list[_FakePageWithInitScript] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def new_page(self) -> _FakePageWithInitScript:
        self.pages_created += 1
        page = _FakePageWithInitScript()
        self.pages.append(page)
        return page


@pytest.mark.asyncio
async def test_stealth_init_script_applied_to_every_new_page(monkeypatch):
    """When stealth is enabled, ``page.add_init_script`` must be
    called for every fresh page created via ``page_factory``.
    """
    transport = BrowserJsonTransport(stealth=True)

    fake_browser = _FakeBrowserWithNewPage()

    # We can't easily test the full iter_ozon_reviews_json loop
    # because it requires a real Ozon payload. Instead, we test the
    # page_factory logic by simulating its behavior.

    # Reach into the transport's iter_ozon_reviews_json to extract
    # the page_factory closure. The simplest way is to monkeypatch
    # the invisible-playwright import to return our fake browser,
    # then drive the iterator until it tries to fetch.

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: fake_browser,
    )

    # Also stub _fetch_json_with_retry to immediately raise, so the
    # iterator creates the initial page (triggering add_init_script)
    # but stops before fetching anything.
    async def fake_fetch(*args, **kwargs):
        raise RuntimeError("stop early")

    monkeypatch.setattr(
        BrowserJsonTransport,
        "_fetch_json_with_retry",
        fake_fetch,
    )

    # Also stub _goto_with_retry so it doesn't try to navigate
    async def fake_goto_retry(*args, **kwargs):
        # Return the most recent page (simulating _goto_with_retry
        # returning the page that completed the goto)
        return fake_browser.pages[-1] if fake_browser.pages else None

    monkeypatch.setattr(
        BrowserJsonTransport,
        "_goto_with_retry",
        fake_goto_retry,
    )

    # Also stub _save_debug
    async def fake_save_debug(*args, **kwargs):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )

    # Drive the iterator — it creates the initial page, the fetch
    # fails, the page is recreated for the single retry, the retry
    # also fails, and the iterator stops the stream silently
    # (no exception propagates; yielding nothing).
    collected = [
        payload
        async for _, payload in transport.iter_ozon_reviews_json(
            product_path="/product/foo-123",
            retry_attempts=1,
        )
    ]
    assert collected == []

    # At least one page should have been created
    assert fake_browser.pages_created >= 1
    # The init script should have been applied to that page
    assert len(fake_browser.pages[0].init_scripts_added) == 1
    # The applied script should be _STEALTH_INIT_SCRIPT
    assert fake_browser.pages[0].init_scripts_added[0] == _STEALTH_INIT_SCRIPT


@pytest.mark.asyncio
async def test_stealth_not_applied_when_disabled(monkeypatch):
    """When stealth=False, add_init_script must NOT be called."""
    transport = BrowserJsonTransport(stealth=False)

    fake_browser = _FakeBrowserWithNewPage()

    monkeypatch.setattr(
        "infrastructure.transports.browser_json."
        "_import_invisible_playwright",
        lambda: lambda **kw: fake_browser,
    )

    async def fake_fetch(*args, **kwargs):
        raise RuntimeError("stop early")
    monkeypatch.setattr(
        BrowserJsonTransport, "_fetch_json_with_retry", fake_fetch,
    )

    async def fake_goto_retry(*args, **kwargs):
        return fake_browser.pages[-1] if fake_browser.pages else None
    monkeypatch.setattr(
        BrowserJsonTransport, "_goto_with_retry", fake_goto_retry,
    )

    async def fake_save_debug(*args, **kwargs):
        return None
    monkeypatch.setattr(
        BrowserJsonTransport, "_save_debug", fake_save_debug,
    )

    collected = [
        payload
        async for _, payload in transport.iter_ozon_reviews_json(
            product_path="/product/foo-123",
            retry_attempts=1,
        )
    ]
    assert collected == []

    assert fake_browser.pages_created >= 1
    # No init scripts should have been applied
    assert len(fake_browser.pages[0].init_scripts_added) == 0


# ---------------------------------------------------------------------------
# response.text() is used for body extraction
# ---------------------------------------------------------------------------


class _FakeResponseWithText:
    """Stand-in for Playwright Response that supports ``.text()``."""

    def __init__(
        self,
        *,
        status: int = 200,
        url: str = "",
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        self.status = status
        self.url = url
        self.headers = headers or {}
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakePageWithResponseText:
    """Stand-in page that supports both goto (returns a response)
    and evaluate (returns body for fallback)."""

    def __init__(
        self,
        *,
        response_body: str = "",
        status: int = 200,
        url: str = "https://www.ozon.ru/api/...",
        content_type: str = "application/json",
        evaluate_body: str = "",  # body returned by document.body.textContent
    ) -> None:
        self._response_body = response_body
        self._status = status
        self._url = url
        self._content_type = content_type
        self._evaluate_body = evaluate_body
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []

    async def goto(self, url: str, **kwargs) -> _FakeResponseWithText:
        self.goto_calls.append(url)
        return _FakeResponseWithText(
            status=self._status,
            url=self._url,
            headers={"content-type": self._content_type},
            body=self._response_body,
        )

    async def evaluate(self, expression: str, *args) -> Any:
        self.evaluate_calls.append(expression)
        # Return the evaluate_body (simulating document.body.textContent
        # when called) — used as a fallback when response.text() is empty
        return self._evaluate_body

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_via_navigation_uses_response_text_first():
    """``response.text()`` must be called before falling back to
    ``page.evaluate(document.body.textContent)``. This is critical
    for Firefox's JSON viewer, which renders the API URL as a
    tree UI instead of raw text.
    """
    transport = BrowserJsonTransport()

    import json as _json
    raw_json = _json.dumps({
        "nextPage": "/product/foo/reviews?page=2",
        "reviews": [{"reviewId": "r1", "rating": 5}],
    })

    page = _FakePageWithResponseText(
        response_body=raw_json,  # raw JSON via response.text()
        # garbage viewer text
        evaluate_body=(
            "JSONRaw DataHeadersSaveCopyCollapse All..."
        ),
        status=200,
        content_type="application/json",
    )

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/product/foo-123/reviews?page=1",
    )

    assert result == {
        "nextPage": "/product/foo/reviews?page=2",
        "reviews": [{"reviewId": "r1", "rating": 5}],
    }
    # Confirm we did NOT fall through to evaluate (response.text was enough)
    # (evaluate may still be called by _read_page_body in fallback
    # path, but the result should have been ignored since
    # response.text() returned non-empty body)


@pytest.mark.asyncio
async def test_fetch_via_navigation_falls_back_to_evaluate_on_empty_text():
    """When ``response.text()`` returns empty (e.g. when the
    response object is for a redirect that was followed
    internally), fall back to ``page.evaluate`` to read the
    rendered body.
    """
    transport = BrowserJsonTransport()

    import json as _json
    raw_json = _json.dumps({
        "reviews": [{"reviewId": "r1", "rating": 5}],
    })

    page = _FakePageWithResponseText(
        response_body="",  # response.text() returns empty
        evaluate_body=raw_json,  # fallback body from DOM
        status=200,
        content_type="application/json",
    )

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/p",
    )

    assert result == {"reviews": [{"reviewId": "r1", "rating": 5}]}


# ---------------------------------------------------------------------------
# Smoke test: JSON viewer body is detected and rejected
# ---------------------------------------------------------------------------


def test_is_cloudflare_challenge_does_not_match_json_viewer_text():
    """The Firefox JSON viewer's rendered body should NOT be
    detected as a Cloudflare challenge — it's a successful response,
    just rendered through the browser's tree UI.
    """
    json_viewer_body = (
        "JSONRaw DataHeadersSaveCopyCollapse AllExpand All (slow)"
        "layout(3)[ {…}, {…}, {…} ]widgetStates"
        '{ "separator-2445819-default-2": \'{"height":34}\' }'
    )
    assert not BrowserJsonTransport._is_cloudflare_challenge(
        json_viewer_body
    )
