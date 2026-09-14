"""Tests for curl_cffi warmup and hybrid transport fallback.

These verify two production scenarios:

1. **curl_cffi warmup**: Before the first API request, the
   transport visits the product page to obtain Cloudflare cookies
   (``__cf_bm``, ``cf_clearance``). Without these cookies, the API
   endpoint returns 403 challenge on every cold-session request.

2. **Hybrid fallback**: When curl_cffi gets a persistent
   ``CloudflareChallengeError`` that retry exhaustion can't
   overcome, the hybrid transport switches to Playwright for
   that page. Playwright runs a real browser that can solve the
   Cloudflare JS challenge and get the ``cf_clearance`` cookie.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from infrastructure.transports.curl_cffi import CurlCffiTransport


# Cache the real asyncio.sleep so test monkeypatches can call it
# without infinite recursion.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(*args, **kwargs):
    return None


# ---------------------------------------------------------------------------
# Stub classes (shared with test_curl_cffi_transport.py)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeAsyncSession:
    def __init__(
        self,
        responses_by_url: dict[str, _FakeResponse] | None = None,
    ) -> None:
        self.responses_by_url = responses_by_url or {}
        self.requests_made: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def get(self, url: str, headers: dict[str, str] | None = None):
        self.requests_made.append((url, headers or {}))
        if url in self.responses_by_url:
            return self.responses_by_url[url]
        return _FakeResponse(
            status_code=404,
            text="not found",
            headers={"content-type": "text/plain"},
        )

    async def close(self) -> None:
        self.closed = True


def _patch_session(monkeypatch, transport: CurlCffiTransport, session: _FakeAsyncSession):
    async def fake_ensure():
        transport._session = session
        return session
    monkeypatch.setattr(transport, "_ensure_session", fake_ensure)


# ---------------------------------------------------------------------------
# Warmup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warmup_visits_product_page_before_first_api_request(monkeypatch):
    """When warmup=True, the first request to iter_ozon_reviews_json
    must be to the product page (not the API), so that Cloudflare
    cookies are obtained.
    """
    transport = CurlCffiTransport(warmup=True)

    product_url = "https://www.ozon.ru/product/foo-123"
    api_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    responses = {
        product_url: _FakeResponse(
            status_code=200,
            text="<html>product page</html>",
            headers={"content-type": "text/html"},
        ),
        api_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "reviews": [{"reviewId": "r1", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    # The first request should have been the product page (warmup).
    assert session.requests_made[0][0] == product_url
    # The second request should have been the API.
    assert session.requests_made[1][0] == api_url
    # Only one API page was fetched (nextPage=None).
    assert pages == [1]


@pytest.mark.asyncio
async def test_warmup_disabled_skips_product_page_visit(monkeypatch):
    """When warmup=False, the first request should be the API URL
    directly — no product page visit.
    """
    transport = CurlCffiTransport(warmup=False)

    api_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    responses = {
        api_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "reviews": [{"reviewId": "r1", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    # Only one request — the API URL. No warmup.
    assert len(session.requests_made) == 1
    assert session.requests_made[0][0] == api_url


@pytest.mark.asyncio
async def test_warmup_only_happens_once_across_iterations(monkeypatch):
    """If iter_ozon_reviews_json is called twice, the warmup should
    only happen on the first call (tracked via _warmed_up flag).
    """
    transport = CurlCffiTransport(warmup=True)

    product_url = "https://www.ozon.ru/product/foo-123"
    api_url_1 = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    responses = {
        product_url: _FakeResponse(
            status_code=200,
            text="<html>product page</html>",
            headers={"content-type": "text/html"},
        ),
        api_url_1: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "reviews": [{"reviewId": "r1", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # First iteration — warmup happens.
    pages = []
    async for page_num, _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    assert transport._warmed_up is True
    first_run_requests = len(session.requests_made)
    # 2 requests: warmup + 1 API page
    assert first_run_requests == 2

    # Second iteration — warmup should NOT happen again.
    pages = []
    async for page_num, _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    # Only 1 new request (the API URL) — no warmup.
    assert len(session.requests_made) == first_run_requests + 1


@pytest.mark.asyncio
async def test_warmup_handles_challenge_response_gracefully(monkeypatch):
    """If the warmup page itself returns a Cloudflare challenge,
    the transport should print a warning and continue (the API
    request will likely also fail, but we don't crash here).
    """
    transport = CurlCffiTransport(warmup=True)

    product_url = "https://www.ozon.ru/product/foo-123"
    api_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    challenge_body = (
        '{"incidentId": "fab_chlg_...", '
        '"challengeURL": "https://www.ozon.ru/challenge.html?..."}'
    )

    responses = {
        product_url: _FakeResponse(
            status_code=403,
            text=challenge_body,
            headers={"content-type": "text/html"},
        ),
        api_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "reviews": [{"reviewId": "r1", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # Should not raise from warmup.
    pages = []
    async for page_num, _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    # Both requests were made despite warmup failing.
    assert len(session.requests_made) == 2
    assert pages == [1]


@pytest.mark.asyncio
async def test_warmup_handles_network_error_gracefully(monkeypatch):
    """If the warmup request itself raises a network error, the
    transport should print a warning and continue to the API.
    """
    transport = CurlCffiTransport(warmup=True)

    product_url = "https://www.ozon.ru/product/foo-123"
    api_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    # Stub the session.get to raise on the product page URL
    class _RaisingSession:
        def __init__(self):
            self.requests_made = []

        async def get(self, url, headers=None):
            self.requests_made.append(url)
            if "product/foo-123" in url and "reviews" not in url:
                raise ConnectionError("warmup network error")
            return _FakeResponse(
                status_code=200,
                text=json.dumps({
                    "nextPage": None,
                    "reviews": [{"reviewId": "r1", "rating": 5}],
                }),
                headers={"content-type": "application/json"},
            )

        async def close(self):
            pass

    session = _RaisingSession()
    _patch_session(monkeypatch, transport, session)  # type: ignore

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # Should not raise even though warmup failed.
    pages = []
    async for page_num, _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    assert pages == [1]


# ---------------------------------------------------------------------------
# Hybrid transport — fallback to Playwright on CloudflareChallengeError
# ---------------------------------------------------------------------------


class _FakeCurlTransport:
    """Stub for CurlCffiTransport that yields pages or raises."""

    def __init__(
        self,
        *,
        page_payloads: list[dict[str, Any]] | None = None,
        raise_on_page: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.page_payloads = list(page_payloads) if page_payloads else []
        self.raise_on_page = raise_on_page
        self.raise_exc = raise_exc
        self.iter_calls = 0
        self._warmed_up = False
        self.warmup = True

    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
        retry_attempts: int = 3,
    ):
        self.iter_calls += 1
        # If this call should raise, raise before yielding anything.
        if (
            self.raise_on_page is not None
            and self.iter_calls == self.raise_on_page
            and self.raise_exc is not None
        ):
            raise self.raise_exc
        # Yield only 1 page per call (since hybrid uses max_pages=1)
        if self.page_payloads:
            yield 1, self.page_payloads.pop(0)

    async def close(self):
        pass


class _FakePlaywrightTransport:
    """Stub for BrowserJsonTransport that yields pages."""

    def __init__(
        self,
        *,
        page_payloads: list[dict[str, Any]] | None = None,
    ) -> None:
        self.page_payloads = list(page_payloads) if page_payloads else []
        self.iter_calls = 0

    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
        retry_attempts: int = 3,
    ):
        self.iter_calls += 1
        if self.page_payloads:
            yield 1, self.page_payloads.pop(0)

    async def close(self):
        pass


def _patch_hybrid_inner_transports(
    monkeypatch,
    hybrid_transport,
    curl_transport,
    playwright_transport,
):
    """Patch a HybridTransport to use fake inner transports."""
    monkeypatch.setattr(
        hybrid_transport, "_curl_transport", curl_transport,
    )
    monkeypatch.setattr(
        hybrid_transport,
        "_playwright_transport",
        playwright_transport,
    )
    # Also patch the lazy factories so they return the fakes too
    monkeypatch.setattr(
        hybrid_transport, "_get_curl_transport", lambda: curl_transport,
    )
    monkeypatch.setattr(
        hybrid_transport,
        "_get_playwright_transport",
        lambda: playwright_transport,
    )


@pytest.mark.asyncio
async def test_hybrid_uses_curl_cffi_when_it_succeeds(monkeypatch):
    """When curl_cffi succeeds on a page, hybrid should yield the
    page and NOT call Playwright.
    """
    from infrastructure.transports.hybrid import HybridTransport

    hybrid = HybridTransport()

    curl_payload = {"nextPage": None, "reviews": [{"reviewId": "r1"}]}
    curl = _FakeCurlTransport(page_payloads=[curl_payload])
    playwright = _FakePlaywrightTransport()
    _patch_hybrid_inner_transports(monkeypatch, hybrid, curl, playwright)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages = []
    async for page_num, payload in hybrid.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append((page_num, payload))

    assert len(pages) == 1
    assert pages[0][1] == curl_payload
    # curl_cffi was used (1 iter call); playwright was NOT (0 iter calls)
    assert curl.iter_calls == 1
    assert playwright.iter_calls == 0
    assert hybrid.last_transport_used == "curl_cffi"


@pytest.mark.asyncio
async def test_hybrid_falls_back_to_playwright_on_cloudflare_challenge(monkeypatch):
    """When curl_cffi raises CloudflareChallengeError, hybrid
    should fall back to Playwright for that page.
    """
    from infrastructure.transports.hybrid import HybridTransport
    from infrastructure.transports.browser_json import (
        CloudflareChallengeError,
    )

    hybrid = HybridTransport()

    curl = _FakeCurlTransport(
        raise_on_page=1,
        raise_exc=CloudflareChallengeError(
            status=403,
            url="https://www.ozon.ru/api/...",
            body="challenge body",
        ),
    )
    pw_payload = {"nextPage": None, "reviews": [{"reviewId": "r1"}]}
    playwright = _FakePlaywrightTransport(page_payloads=[pw_payload])
    _patch_hybrid_inner_transports(monkeypatch, hybrid, curl, playwright)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages = []
    async for page_num, payload in hybrid.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append((page_num, payload))

    assert len(pages) == 1
    assert pages[0][1] == pw_payload
    # Both were tried
    assert curl.iter_calls == 1
    assert playwright.iter_calls == 1
    assert hybrid.last_transport_used == "playwright"


@pytest.mark.asyncio
async def test_hybrid_falls_back_to_playwright_on_any_error(monkeypatch):
    """Hybrid should fall back to Playwright on ANY exception from
    curl_cffi, not just CloudflareChallengeError. This protects
    against unexpected curl_cffi crashes.
    """
    from infrastructure.transports.hybrid import HybridTransport

    hybrid = HybridTransport()

    curl = _FakeCurlTransport(
        raise_on_page=1,
        raise_exc=RuntimeError("unexpected curl_cffi crash"),
    )
    pw_payload = {"nextPage": None, "reviews": [{"reviewId": "r1"}]}
    playwright = _FakePlaywrightTransport(page_payloads=[pw_payload])
    _patch_hybrid_inner_transports(monkeypatch, hybrid, curl, playwright)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages = []
    async for page_num, payload in hybrid.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append((page_num, payload))

    assert len(pages) == 1
    assert pages[0][1] == pw_payload
    assert hybrid.last_transport_used == "playwright"


@pytest.mark.asyncio
async def test_hybrid_raises_when_both_transports_fail(monkeypatch):
    """If both curl_cffi and Playwright fail to produce a page,
    hybrid should raise RuntimeError.
    """
    from infrastructure.transports.hybrid import HybridTransport
    from infrastructure.transports.browser_json import (
        CloudflareChallengeError,
    )

    hybrid = HybridTransport()

    curl = _FakeCurlTransport(
        raise_on_page=1,
        raise_exc=CloudflareChallengeError(
            status=403,
            url="https://www.ozon.ru/api/...",
            body="challenge body",
        ),
    )
    # Playwright returns no pages (empty iterator)
    playwright = _FakePlaywrightTransport(page_payloads=[])
    _patch_hybrid_inner_transports(monkeypatch, hybrid, curl, playwright)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(RuntimeError, match="оба транспорта"):
        async for _ in hybrid.iter_ozon_reviews_json(
            product_path="/product/foo-123",
            retry_attempts=1,
        ):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_hybrid_multiple_pages_alternates_transports(monkeypatch):
    """Hybrid should try curl_cffi first on every page, even after
    a previous page required Playwright fallback. (Cloudflare
    cookies are session-bound; the curl_cffi session is separate
    from Playwright's.)
    """
    from infrastructure.transports.hybrid import HybridTransport
    from infrastructure.transports.browser_json import (
        CloudflareChallengeError,
    )

    hybrid = HybridTransport()

    # Page 1: curl_cffi fails with challenge, playwright succeeds.
    # Page 2: curl_cffi succeeds (cookies may have been set by
    # playwright, but they're separate sessions).
    # Page 3: nextPage=None, stop.

    page1_pw = {"nextPage": "/product/foo/reviews?page=2", "reviews": [{"reviewId": "r1"}]}
    page2_curl = {"nextPage": None, "reviews": [{"reviewId": "r2"}]}

    # The hybrid transport calls curl_cffi.iter_ozon_reviews_json
    # with max_pages=1, so each call yields at most 1 page. We need
    # curl_cffi to fail on the first call and succeed on the second.
    curl = _FakeCurlTransport(
        page_payloads=[page2_curl],  # only the second page succeeds
        raise_on_page=1,
        raise_exc=CloudflareChallengeError(
            status=403, url="api", body="challenge",
        ),
    )
    playwright = _FakePlaywrightTransport(page_payloads=[page1_pw])
    _patch_hybrid_inner_transports(monkeypatch, hybrid, curl, playwright)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages = []
    async for page_num, payload in hybrid.iter_ozon_reviews_json(
        product_path="/product/foo",
        retry_attempts=1,
    ):
        pages.append((page_num, payload))

    # Two pages yielded: page 1 from playwright, page 2 from curl_cffi
    assert len(pages) == 2
    assert pages[0][1] == page1_pw
    assert pages[1][1] == page2_curl


@pytest.mark.asyncio
async def test_hybrid_close_releases_both_transports(monkeypatch):
    """close() should call close() on both inner transports."""
    from infrastructure.transports.hybrid import HybridTransport

    hybrid = HybridTransport()

    curl_closed = {"done": False}
    playwright_closed = {"done": False}

    class _CurlClosable:
        async def close(self):
            curl_closed["done"] = True

    class _PlaywrightClosable:
        async def close(self):
            playwright_closed["done"] = True

    monkeypatch.setattr(
        hybrid, "_curl_transport", _CurlClosable(),
    )
    monkeypatch.setattr(
        hybrid, "_playwright_transport", _PlaywrightClosable(),
    )

    await hybrid.close()

    assert curl_closed["done"] is True
    assert playwright_closed["done"] is True


def test_hybrid_default_construction():
    """HybridTransport can be constructed without any args and
    lazily creates inner transports on first use.
    """
    from infrastructure.transports.hybrid import HybridTransport

    hybrid = HybridTransport()
    assert hybrid._curl_transport is None
    assert hybrid._playwright_transport is None
    assert hybrid.last_transport_used is None
