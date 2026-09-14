"""Tests for the curl_cffi-based transport.

These tests stub out the curl_cffi ``AsyncSession`` so they don't
make real HTTP requests. They verify:

- Transport construction and defaults
- Pagination iterator follows ``nextPage`` links until exhausted
- HTTP 403 with a Cloudflare challenge body raises
  ``CloudflareChallengeError`` (so the caller's retry_on filter
  catches it)
- HTTP 500 raises plain ``RuntimeError``
- JSON decode failure raises ``RuntimeError``
- ``iter_ozon_reviews_by_scroll`` raises ``NotImplementedError``
- ``iter_all_ozon_reviews`` yields all unique reviews across pages
  with cross-page dedup
- ``max_reviews`` cap is respected across pages
- ``max_pages`` cap is respected
- Static helpers: ``_build_api_url``, ``extract_next_path``,
  ``_review_node_id``, ``_is_cloudflare_challenge``
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from infrastructure.transports.curl_cffi import (
    CurlCffiTransport,
    _DEFAULT_HEADERS,
    _IMPERSONATE_TARGETS,
)


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------


def test_transport_defaults():
    t = CurlCffiTransport()
    assert t.impersonate == "chrome120"
    assert t.timeout == 30.0
    assert t.proxy is None
    assert t.max_redirects == 10
    # Headers should include the Chrome 120 default set
    assert "Accept-Language" in t.headers
    assert "ru-RU" in t.headers["Accept-Language"]
    assert "Sec-Fetch-Mode" in t.headers


def test_transport_custom_impersonate():
    t = CurlCffiTransport(impersonate="firefox120")
    assert t.impersonate == "firefox120"


def test_transport_custom_proxy():
    t = CurlCffiTransport(proxy="http://proxy:8080")
    assert t.proxy == "http://proxy:8080"


def test_transport_custom_headers_merge():
    """Custom headers should merge with defaults, not replace."""
    t = CurlCffiTransport(headers={"X-Custom": "yes"})
    assert t.headers["X-Custom"] == "yes"
    # Defaults still present
    assert "Accept-Language" in t.headers


def test_impersonate_targets_includes_chrome120():
    """The default impersonate target (chrome120) should be in the
    list of supported targets.
    """
    assert "chrome120" in _IMPERSONATE_TARGETS


def test_default_headers_has_chrome_ua_markers():
    """The default headers should match a real Chrome 120
    navigation request."""
    assert "sec-ch-ua" in {k.lower() for k in _DEFAULT_HEADERS}
    assert "sec-fetch-mode" in {k.lower() for k in _DEFAULT_HEADERS}


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def test_build_api_url_includes_endpoint_and_internal_path():
    url = CurlCffiTransport._build_api_url(
        "/product/foo-123/reviews?page=1",
    )
    assert "entrypoint-api.bx/page/json/v2" in url
    assert "url=%2Fproduct%2Ffoo-123%2Freviews" in url


def test_extract_next_path_with_string():
    payload = {"nextPage": "/product/foo/reviews?page=2"}
    assert (
        CurlCffiTransport.extract_next_path(payload)
        == "/product/foo/reviews?page=2"
    )


def test_extract_next_path_with_dict():
    payload = {"nextPage": {"url": "/product/foo/reviews?page=3"}}
    assert (
        CurlCffiTransport.extract_next_path(payload)
        == "/product/foo/reviews?page=3"
    )


def test_extract_next_path_with_none():
    payload = {"nextPage": None}
    assert CurlCffiTransport.extract_next_path(payload) is None


def test_extract_next_path_with_empty_string():
    payload = {"nextPage": ""}
    assert CurlCffiTransport.extract_next_path(payload) is None


def test_review_node_id_prefers_review_id():
    node = {"reviewId": "r1", "uuid": "u1", "id": "i1"}
    assert CurlCffiTransport._review_node_id(node) == "r1"


def test_review_node_id_falls_back_to_uuid():
    node = {"uuid": "u1", "id": "i1"}
    assert CurlCffiTransport._review_node_id(node) == "u1"


def test_review_node_id_returns_none_when_no_id_keys():
    node = {"foo": "bar"}
    assert CurlCffiTransport._review_node_id(node) is None


# ---------------------------------------------------------------------------
# _is_cloudflare_challenge (mirrors browser_json tests)
# ---------------------------------------------------------------------------


def test_is_cloudflare_challenge_detects_json_envelope():
    body = (
        '{"incidentId": "fab_chlg_...", '
        '"challengeURL": "https://www.ozon.ru/challenge.html?..."}'
    )
    assert CurlCffiTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_detects_html_enable_javascript():
    """The HTML challenge page with 'enable JavaScript' should be
    detected (mirrors the browser_json test for the same body)."""
    html_body = """
    <div>
        <h2>Пожалуйста, включите JavaScript для продолжения</h2>
        <span>Нам нужно убедиться, что вы не робот.</span>
        ID: fab_chlg_2026_abc
    </div>
    """
    assert CurlCffiTransport._is_cloudflare_challenge(html_body)


def test_is_cloudflare_challenge_rejects_normal_json():
    body = '{"reviews": [{"reviewId": "r1", "rating": 5}]}'
    assert not CurlCffiTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_rejects_empty_body():
    assert not CurlCffiTransport._is_cloudflare_challenge("")


# ---------------------------------------------------------------------------
# Async session stubs
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for a curl_cffi Response object."""

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
    """Stand-in for a curl_cffi AsyncSession.

    Each URL configured in ``responses_by_url`` returns the canned
    response. Other URLs return a 404.
    """

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
    """Patch a transport to use a fake AsyncSession."""
    async def fake_ensure():
        transport._session = session
        return session
    monkeypatch.setattr(transport, "_ensure_session", fake_ensure)


# ---------------------------------------------------------------------------
# iter_ozon_reviews_json — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_follows_next_page(monkeypatch):
    """The iterator should fetch page 1, follow nextPage to page 2,
    then stop when nextPage is None.
    """
    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )
    page2_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D2"
    )

    responses = {
        page1_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": "/product/foo-123/reviews?page=2",
                "reviews": [{"reviewId": "r1", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
        page2_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "reviews": [{"reviewId": "r2", "rating": 4}],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    # Disable warmup for this test — it's a pure HTTP test of the
    # pagination iterator. Warmup is tested separately.
    transport.warmup = False

    # Speed up the retry sleep (in case it's triggered)
    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pages_yielded = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        start_page=1,
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    assert len(pages_yielded) == 2
    assert pages_yielded[0][0] == 1
    assert pages_yielded[1][0] == 2
    # Both API URLs were fetched (no warmup since warmup=False)
    assert len(session.requests_made) == 2


# ---------------------------------------------------------------------------
# iter_ozon_reviews_json — HTTP 403 with Cloudflare challenge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_raises_cloudflare_challenge(monkeypatch):
    """HTTP 403 with a Cloudflare challenge body raises
    CloudflareChallengeError (which is a RuntimeError subclass).
    """
    from infrastructure.transports.browser_json import (
        CloudflareChallengeError,
    )

    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    challenge_body = (
        '{"incidentId": "fab_chlg_...", '
        '"challengeURL": "https://www.ozon.ru/challenge.html?..."}'
    )

    responses = {
        page1_url: _FakeResponse(
            status_code=403,
            text=challenge_body,
            headers={"content-type": "text/html"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(CloudflareChallengeError) as exc_info:
        async for _ in transport.iter_ozon_reviews_json(
            product_path="/product/foo-123",
            start_page=1,
            retry_attempts=1,  # 1 attempt → no retry, raise immediately
        ):
            pass  # pragma: no cover

    assert exc_info.value.status == 403


# ---------------------------------------------------------------------------
# iter_ozon_reviews_json — HTTP 500 raises RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_raises_runtime_error_on_500(monkeypatch):
    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    responses = {
        page1_url: _FakeResponse(
            status_code=500,
            text="Internal Server Error",
            headers={"content-type": "text/plain"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        async for _ in transport.iter_ozon_reviews_json(
            product_path="/product/foo-123",
            start_page=1,
            retry_attempts=1,
        ):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# iter_ozon_reviews_json — JSON decode failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_raises_on_non_json_body(monkeypatch):
    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    responses = {
        page1_url: _FakeResponse(
            status_code=200,
            text="<html>not JSON</html>",
            headers={"content-type": "text/html"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(RuntimeError, match="не удалось декодировать"):
        async for _ in transport.iter_ozon_reviews_json(
            product_path="/product/foo-123",
            start_page=1,
            retry_attempts=1,
        ):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# iter_ozon_reviews_by_scroll — NOT supported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_by_scroll_raises_not_implemented():
    transport = CurlCffiTransport()

    with pytest.raises(NotImplementedError, match="scroll"):
        async for _ in transport.iter_ozon_reviews_by_scroll(
            product_path="/p",
        ):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# iter_all_ozon_reviews — pagination only, dedup across pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_ozon_reviews_dedup_across_pages(monkeypatch):
    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )
    page2_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D2"
    )

    # page 1 has r1, r2; page 2 has r2 (dup) and r3
    responses = {
        page1_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": "/product/foo-123/reviews?page=2",
                "_review_nodes": [
                    {"reviewId": "r1", "rating": 5, "text": "a"},
                    {"reviewId": "r2", "rating": 4, "text": "b"},
                ],
            }),
            headers={"content-type": "application/json"},
        ),
        page2_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "_review_nodes": [
                    {"reviewId": "r2", "rating": 4, "text": "b"},  # dup
                    {"reviewId": "r3", "rating": 5, "text": "c"},
                ],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    yielded = []
    async for strategy, node in transport.iter_all_ozon_reviews(
        product_path="/product/foo-123",
        retry_attempts=1,
        page_delay_seconds=0,
    ):
        yielded.append((strategy, node.get("reviewId")))

    # All strategy values are "pagination"
    assert all(s == "pagination" for s, _ in yielded)
    # r2 should appear only once (from page 1)
    ids = [rid for _, rid in yielded]
    assert ids == ["r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# iter_all_ozon_reviews — max_reviews cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_ozon_reviews_max_reviews_cap(monkeypatch):
    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )

    responses = {
        page1_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "_review_nodes": [
                    {"reviewId": f"r{i}", "rating": 5, "text": "a"}
                    for i in range(10)
                ],
            }),
            headers={"content-type": "application/json"},
        ),
    }

    session = _FakeAsyncSession(responses_by_url=responses)
    _patch_session(monkeypatch, transport, session)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    yielded = []
    async for strategy, node in transport.iter_all_ozon_reviews(
        product_path="/product/foo-123",
        max_reviews=3,
        retry_attempts=1,
        page_delay_seconds=0,
    ):
        yielded.append(node.get("reviewId"))

    assert len(yielded) == 3
    assert yielded == ["r0", "r1", "r2"]


# ---------------------------------------------------------------------------
# iter_ozon_reviews_json — max_pages cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_max_pages_cap(monkeypatch):
    transport = CurlCffiTransport()

    page1_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D1"
    )
    page2_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D2"
    )
    page3_url = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?"
        "url=%2Fproduct%2Ffoo-123%2Freviews%3Fpage%3D3"
    )

    responses = {
        page1_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": "/product/foo-123/reviews?page=2",
                "reviews": [{"reviewId": "r1", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
        page2_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": "/product/foo-123/reviews?page=3",
                "reviews": [{"reviewId": "r2", "rating": 5}],
            }),
            headers={"content-type": "application/json"},
        ),
        page3_url: _FakeResponse(
            status_code=200,
            text=json.dumps({
                "nextPage": None,
                "reviews": [{"reviewId": "r3", "rating": 5}],
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
        max_pages=2,  # stop after 2 pages
        retry_attempts=1,
    ):
        pages.append(page_num)

    # Should stop after page 2 (max_pages=2)
    assert pages == [1, 2]
    # Page 3 should NOT have been fetched
    assert page3_url not in [url for url, _ in session.requests_made]


# ---------------------------------------------------------------------------
# close() releases the session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_releases_session(monkeypatch):
    transport = CurlCffiTransport()
    session = _FakeAsyncSession()
    transport._session = session

    await transport.close()
    assert session.closed is True
    assert transport._session is None


@pytest.mark.asyncio
async def test_close_no_op_when_no_session():
    transport = CurlCffiTransport()
    # Should not raise
    await transport.close()
    assert transport._session is None


# ---------------------------------------------------------------------------
# Context manager protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_context_manager(monkeypatch):
    transport = CurlCffiTransport()
    session = _FakeAsyncSession()
    # Patch _ensure_session to return our fake
    async def fake_ensure():
        transport._session = session
        return session
    monkeypatch.setattr(transport, "_ensure_session", fake_ensure)

    async with transport as t:
        assert t is transport
        assert transport._session is session

    # After exit, session should be closed
    assert session.closed is True
