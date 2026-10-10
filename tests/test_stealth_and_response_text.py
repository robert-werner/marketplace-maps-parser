"""Tests for engine-owned stealth and raw response body extraction."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from infrastructure.transports.browser_json import BrowserJsonTransport

_REAL_SLEEP = asyncio.sleep


def test_transport_stealth_is_engine_owned_by_default() -> None:
    assert BrowserJsonTransport().stealth is True


def test_transport_stealth_compatibility_flag_is_retained() -> None:
    assert BrowserJsonTransport(stealth=False).stealth is False


class _FakePageWithInitScript:
    def __init__(self) -> None:
        self.init_scripts_added: list[str] = []
        self.closed = False

    async def add_init_script(self, script: str) -> None:
        self.init_scripts_added.append(script)

    async def goto(self, url: str, **kwargs: Any) -> Any:
        return None

    async def evaluate(self, expression: str, *args: Any) -> Any:
        return ""

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserWithNewPage:
    def __init__(self) -> None:
        self.pages_created = 0
        self.pages: list[_FakePageWithInitScript] = []

    async def __aenter__(self) -> _FakeBrowserWithNewPage:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def new_page(self) -> _FakePageWithInitScript:
        self.pages_created += 1
        page = _FakePageWithInitScript()
        self.pages.append(page)
        return page


@pytest.mark.asyncio
async def test_transport_does_not_add_a_page_level_fingerprint_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = BrowserJsonTransport(stealth=True)
    fake_browser = _FakeBrowserWithNewPage()
    monkeypatch.setattr(
        "infrastructure.transports.browser_json._import_invisible_playwright",
        lambda: lambda **kw: fake_browser,
    )

    async def fake_fetch(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("stop early")

    async def fake_goto(*args: Any, **kwargs: Any) -> Any:
        return fake_browser.pages[-1]

    async def fake_debug(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        BrowserJsonTransport, "_fetch_json_with_retry", fake_fetch,
    )
    monkeypatch.setattr(BrowserJsonTransport, "_goto_with_retry", fake_goto)
    monkeypatch.setattr(BrowserJsonTransport, "_save_debug", fake_debug)

    with pytest.raises(RuntimeError, match="browser recreation"):
        [
            payload
            async for _, payload in transport.iter_ozon_reviews_json(
                product_path="/product/foo-123", retry_attempts=1,
            )
        ]
    assert fake_browser.pages_created >= 1
    assert fake_browser.pages[0].init_scripts_added == []


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
