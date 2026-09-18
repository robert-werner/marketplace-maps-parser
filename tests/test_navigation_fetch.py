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

from infrastructure.transports.browser_json.transport import (
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
async def test_fetch_via_navigation_raises_cloudflare_challenge_on_403(
    monkeypatch,
):
    """HTTP 403 with a challenge body raises CloudflareChallengeError.

    The challenge page never resolves (the fake page always returns
    the same body), so the wait times out and the caller raises
    CloudflareChallengeError.
    """
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

    # Speed up the test by patching asyncio.sleep so the challenge
    # wait loop doesn't actually wait 30 seconds.
    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # Also patch _wait_for_challenge_completion to use a tiny timeout
    # so the test doesn't loop 60 times.
    async def fast_wait(*args, **kwargs):
        # Return the initial values immediately — challenge didn't
        # resolve, so the caller should raise CloudflareChallengeError.
        return (
            kwargs.get("initial_body", challenge_body),
            kwargs.get("initial_status", 403),
            kwargs.get("initial_url", "https://www.ozon.ru/api/..."),
            kwargs.get("initial_content_type", "text/html"),
        )
    monkeypatch.setattr(
        transport, "_wait_for_challenge_completion", fast_wait,
    )

    with pytest.raises(CloudflareChallengeError) as exc_info:
        await transport._fetch_json_via_navigation(
            page=page,
            internal_path="/p",
        )

    assert exc_info.value.status == 403
    assert "incidentId" in exc_info.value.body


# ---------------------------------------------------------------------------
# Cloudflare HTML challenge page detection
# ---------------------------------------------------------------------------


def test_is_cloudflare_challenge_detects_html_enable_javascript():
    """The new HTML challenge body from Cloudflare contains
    'enable JavaScript' / 'включите JavaScript' markers and should
    be detected as a Cloudflare challenge.
    """
    html_body = """
    <div class="container">
        <div class="message">
            <div class="variant">
                <h2 class="h2">
                    Пожалуйста, включите JavaScript
                    для продолжения
                </h2>
                <span class="subtitle">
                    Нам нужно убедиться, что вы не робот.
                </span>
            </div>
            <div class="variant" lang="en">
                <h2 class="h2">Please, enable JavaScript to continue</h2>
                <span class="subtitle">
                    We need to make sure that you are not a robot.
                </span>
            </div>
        </div>
        <div class="details">
            <span class="details-text">
                <b>ID:</b>
                fab_chlg_20260914183121_01M2GK090SFR62ZB347M17WHM0
            </span>,
            <span class="details-text"><b>IP:</b> 85.95.182.24</span>,
        </div>
    </div>
    """
    assert BrowserJsonTransport._is_cloudflare_challenge(html_body)


def test_is_cloudflare_challenge_detects_fab_chlg_prefix():
    """The ``fab_chlg_`` incident ID prefix is a reliable marker."""
    body = "some random HTML with ID: fab_chlg_2026_abc123"
    assert BrowserJsonTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_detects_russian_text():
    """Russian text 'Нам нужно убедиться, что вы не робот' is detected."""
    body = "Нам нужно убедиться, что вы не робот"
    assert BrowserJsonTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_detects_english_text():
    """English text 'We need to make sure that you are not a robot'."""
    body = "We need to make sure that you are not a robot"
    assert BrowserJsonTransport._is_cloudflare_challenge(body)


# ---------------------------------------------------------------------------
# Challenge wait — challenge resolves to JSON
# ---------------------------------------------------------------------------


class _FakePageChallengeResolving:
    """A fake page that first returns the challenge body, then after
    a few reads returns the JSON body (simulating the Cloudflare JS
    challenge completing and redirecting)."""

    def __init__(
        self,
        *,
        challenge_body: str,
        json_body: str,
        resolve_after_reads: int = 2,
    ) -> None:
        self._challenge_body = challenge_body
        self._json_body = json_body
        self._resolve_after = resolve_after_reads
        self._read_count = 0
        self.goto_calls = 0

    async def goto(self, url: str, **kwargs) -> _FakeResponse:
        self.goto_calls += 1
        # Initial goto returns the challenge page (403)
        return _FakeResponse(
            status=403,
            url=url,
            headers={"content-type": "text/html"},
        )

    async def evaluate(self, expression: str, *args) -> Any:
        self._read_count += 1
        if self._read_count <= self._resolve_after:
            return self._challenge_body
        return self._json_body

    @property
    def url(self) -> str:
        return "https://www.ozon.ru/api/entrypoint-api.bx/..."

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_via_navigation_waits_for_challenge_to_resolve(
    monkeypatch,
):
    """When Cloudflare returns the HTML challenge page, we should
    wait for the embedded JS to solve the challenge and redirect
    to the actual JSON. After the challenge resolves, the body
    changes to JSON and we return it.
    """
    transport = BrowserJsonTransport()

    challenge_body = (
        '<h2>Пожалуйста, включите JavaScript для продолжения</h2>'
        '<span>Нам нужно убедиться, что вы не робот.</span>'
        '<span>ID: fab_chlg_2026_abc</span>'
    )
    import json as _json
    json_body = _json.dumps({
        "nextPage": "/product/foo/reviews?page=2",
        "reviews": [{"reviewId": "r1", "rating": 5}],
    })

    page = _FakePageChallengeResolving(
        challenge_body=challenge_body,
        json_body=json_body,
        resolve_after_reads=2,  # challenge resolves on 3rd body read
    )

    # Speed up the wait loop
    async def _fast_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/product/foo-123/reviews?page=1",
    )

    assert result == {
        "nextPage": "/product/foo/reviews?page=2",
        "reviews": [{"reviewId": "r1", "rating": 5}],
    }


# ---------------------------------------------------------------------------
# Challenge wait — challenge never resolves
# (timeout → CloudflareChallengeError)
# ---------------------------------------------------------------------------


class _FakePageChallengeNeverResolves:
    """A fake page that always returns the challenge body (simulating
    the JS challenge failing to complete)."""

    def __init__(self, *, challenge_body: str) -> None:
        self._challenge_body = challenge_body
        self.goto_calls = 0

    async def goto(self, url: str, **kwargs) -> _FakeResponse:
        self.goto_calls += 1
        return _FakeResponse(
            status=403,
            url=url,
            headers={"content-type": "text/html"},
        )

    async def evaluate(self, expression: str, *args) -> Any:
        return self._challenge_body

    @property
    def url(self) -> str:
        return "https://www.ozon.ru/api/entrypoint-api.bx/..."

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_via_navigation_raises_when_challenge_never_resolves(
    monkeypatch,
):
    """When the Cloudflare challenge never resolves (JS fails to
    execute or times out), we should raise CloudflareChallengeError
    after the max wait timeout."""
    transport = BrowserJsonTransport()

    challenge_body = (
        '<h2>Пожалуйста, включите JavaScript для продолжения</h2>'
        '<span>ID: fab_chlg_2026_abc</span>'
    )

    page = _FakePageChallengeNeverResolves(
        challenge_body=challenge_body,
    )

    # Speed up the wait loop — don't actually sleep
    async def _fast_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    # Patch _wait_for_challenge_completion to use a tiny max_wait
    # so the test doesn't loop forever

    async def fast_wait(*args, **kwargs):
        # Simulate the challenge timing out immediately
        return (
            kwargs["initial_body"],
            kwargs["initial_status"],
            kwargs["initial_url"],
            kwargs["initial_content_type"],
        )
    monkeypatch.setattr(
        transport, "_wait_for_challenge_completion", fast_wait,
    )

    with pytest.raises(CloudflareChallengeError):
        await transport._fetch_json_via_navigation(
            page=page,
            internal_path="/p",
        )


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
async def test_fetch_json_inside_page_uses_legacy_fetch_when_configured(
    monkeypatch,
):
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


# ---------------------------------------------------------------------------
# Speed optimizations: fast re-navigation on a lost body
# ---------------------------------------------------------------------------


class _FakeResponseWithText:
    """Response stand-in whose ``text()`` works."""

    def __init__(self, *, body: str, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.url = "https://www.ozon.ru/api/..."
        self.headers = {"content-type": "application/json"}

    async def text(self) -> str:
        return self._body


class _FakePageLosingFirstBody:
    """First navigation loses the response body (``response.text()``
    is unavailable and the DOM fallback renders the Firefox JSON
    viewer's UI text); the SECOND navigation of the same URL returns
    the raw JSON. Mirrors the production failure mode measured in
    the 2026-09-16 logs."""

    def __init__(self, *, json_body: str) -> None:
        self._json_body = json_body
        self.goto_calls = 0

    async def goto(self, url: str, **kwargs) -> Any:
        self.goto_calls += 1
        if self.goto_calls == 1:
            # No text() on purpose: response.text() raises
            # AttributeError and the DOM fallback reads viewer text.
            return _FakeResponse(status=200, url=url)
        return _FakeResponseWithText(body=self._json_body)

    async def evaluate(self, expression: str, *args) -> Any:
        # DOM fallback during the broken first navigation.
        return (
            "JSONRaw DataHeadersSaveCopyCollapse AllExpand All "
            "(slow)layout(3)widgetStates..."
        )

    async def wait_for_timeout(self, ms: int) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_via_navigation_renavigates_immediately_when_body_lost():
    """A lost body on the first navigation must trigger ONE immediate
    re-navigation (no retry_async backoff) that recovers the JSON."""
    import json as _json

    transport = BrowserJsonTransport()
    payload = {"reviews": [{"reviewId": "r1", "rating": 5}]}
    json_body = _json.dumps(payload)

    page = _FakePageLosingFirstBody(json_body=json_body)

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/p",
    )

    assert result == payload
    # Exactly two navigations: the lost one + the immediate retry.
    assert page.goto_calls == 2


@pytest.mark.asyncio
async def test_fetch_via_navigation_no_renavigation_for_json_body():
    """A healthy first navigation must not pay the extra goto."""
    import json as _json

    transport = BrowserJsonTransport()
    payload = {"reviews": [{"reviewId": "r1", "rating": 5}]}

    page = _FakePageLosingFirstBody(json_body=_json.dumps(payload))
    # Make the FIRST response healthy too.
    async def healthy_goto(url: str, **kwargs) -> Any:
        page.goto_calls += 1
        return _FakeResponseWithText(body=page._json_body)

    page.goto = healthy_goto  # type: ignore[method-assign]

    result = await transport._fetch_json_via_navigation(
        page=page,
        internal_path="/p",
    )

    assert result == payload
    assert page.goto_calls == 1


# ---------------------------------------------------------------------------
# Speed optimizations: tab reuse in _goto_with_retry
# ---------------------------------------------------------------------------


class _FakePageCountingGoto:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_goto_with_retry_reuses_passed_page_on_first_attempt():
    """The first attempt navigates the CALLER's tab (one user-like
    tab per stream); page_factory is not called."""
    transport = BrowserJsonTransport()
    existing_page = _FakePageCountingGoto()
    factory_calls = {"n": 0}

    async def page_factory() -> Any:
        factory_calls["n"] += 1
        return _FakePageCountingGoto()

    result = await transport._goto_with_retry(
        page_factory=page_factory,
        page=existing_page,
        reviews_url="https://www.ozon.ru/product/foo/reviews?page=2",
        attempts=3,
    )

    assert result is existing_page
    assert factory_calls["n"] == 0
    assert len(existing_page.goto_calls) == 1


@pytest.mark.asyncio
async def test_goto_with_retry_recreates_page_after_failure(monkeypatch):
    """A failing first attempt on the reused tab falls back to a
    fresh page from the factory for the retry."""
    from infrastructure.transports import browser_json as bj

    transport = BrowserJsonTransport()
    broken_page = _FakePageCountingGoto()

    async def broken_goto(url: str, **kwargs) -> None:
        raise RuntimeError("net::ERR_CONNECTION_RESET")

    broken_page.goto = broken_goto  # type: ignore[method-assign]

    factory_calls = {"n": 0}

    async def page_factory() -> Any:
        factory_calls["n"] += 1
        return _FakePageCountingGoto()

    monkeypatch.setattr(bj, "_retryable_errors", lambda: (RuntimeError,))
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    result = await transport._goto_with_retry(
        page_factory=page_factory,
        page=broken_page,
        reviews_url="https://www.ozon.ru/product/foo/reviews?page=2",
        attempts=3,
    )

    assert result is not broken_page
    assert factory_calls["n"] == 1
    assert len(result.goto_calls) == 1


# ---------------------------------------------------------------------------
# Speed optimizations: short settle for the navigation strategy
# ---------------------------------------------------------------------------


class _FakePageRecordingWaits:
    def __init__(self) -> None:
        self.wait_calls: list[int] = []

    async def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)


@pytest.mark.asyncio
async def test_settle_after_goto_is_short_for_navigation():
    """Navigation strategy waits only a 500ms beacon grace, not the
    full settle_ms — the API goto replaces the page content anyway."""
    transport = BrowserJsonTransport(
        settle_ms=5000,
        fetch_strategy="navigation",
    )
    page = _FakePageRecordingWaits()

    await transport._settle_after_goto(page)

    assert page.wait_calls == [500]


@pytest.mark.asyncio
async def test_settle_after_goto_is_full_for_fetch_strategy():
    """Fetch strategy keeps the full settle: the page's JS context
    must be warm before fetch() runs from it."""
    transport = BrowserJsonTransport(
        settle_ms=2000,
        fetch_strategy="fetch",
    )
    page = _FakePageRecordingWaits()

    await transport._settle_after_goto(page)

    assert page.wait_calls == [2000]
