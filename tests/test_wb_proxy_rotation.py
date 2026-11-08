"""Tests for proxy rotation in iter_review_batches."""
from unittest.mock import patch

import pytest


class _FakePage:
    async def goto(self, *a, **kw):
        return _FakeResponse(status=200)
    async def evaluate(self, *a, **kw):
        return {"cards": []}
    async def evaluate_script(self, *a, **kw):
        return None
    async def wait_for(self, *a, **kw):
        return None
    async def scroll_into_view_if_needed(self, *a, **kw):
        pass
    async def wait_for_timeout(self, *a, **kw):
        pass
    def __call__(self):
        return _FakeLocator()


class _FakeLocator:
    async def wait_for(self, *a, **kw):
        return None
    async def count(self):
        return 0
    async def first(self):
        return self
    async def nth(self, idx):
        return self
    async def scroll_into_view_if_needed(self):
        pass
    async def inner_text(self):
        return ""
    def query_selector_all(self, *a):
        return []


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status
    async def json(self):
        return {}


@pytest.mark.asyncio
async def test_rotation_creates_new_browser():
    """Block triggers browser restart with next proxy."""
    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
    ) as mock_ip:
        created = []

        def fake_ctor(proxy=None, seed=None, humanize=None):
            created.append(proxy)
            return _FakeBrowser()

        mock_ip.return_value = fake_ctor

        from infrastructure.transports.wb_browser import (
            WildberriesBrowserTransport, HTTP498Error,
        )
        from infrastructure.transports.proxy_pool import ProxyPool

        calls = [0]

        async def _patched_iter(self, product_url):
            calls[0] += 1
            if calls[0] == 1:
                raise HTTP498Error("HTTP 498")
            return
            yield  # unreachable, keeps generator as async gen

        with patch.object(
            WildberriesBrowserTransport,
            "_iter_product_and_reviews",
            _patched_iter, create=True,
        ):
            transport = WildberriesBrowserTransport(
                product_url="https://test.example.com",
                proxy_pool=ProxyPool.from_list([
                    {"server": "http://a.b:1080"},
                    {"server": "http://c.d:2080"},
                ]),
            )
            try:
                async for _ in transport.iter_review_batches(
                    "https://test.example.com",
                ):
                    pass
            except HTTP498Error:
                pass

        assert len(created) >= 2, f"created={created}"
        assert created[1] is not created[0]


@pytest.mark.asyncio
async def test_no_pool_propagates_block():
    """Without proxy_pool, block error is raised immediately."""
    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
    ) as mock_ip:
        created = []

        def fake_ctor(proxy=None, **kw):
            created.append(proxy)
            return _FakeBrowser()

        mock_ip.return_value = fake_ctor

        from infrastructure.transports.wb_browser import (
            WildberriesBrowserTransport, HTTP498Error,
        )

        async def _patched_iter(self, product_url):
            raise HTTP498Error("HTTP 498")
            return
            yield  # async gen

        with patch.object(
            WildberriesBrowserTransport,
            "_iter_product_and_reviews",
            _patched_iter, create=True,
        ):
            transport = WildberriesBrowserTransport(
                product_url="https://test.example.com",
            )
            with pytest.raises(HTTP498Error):
                async for _ in transport.iter_review_batches(
                    "https://test.example.com",
                ):
                    pass
        assert len(created) == 1


class _FakeBrowser:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def new_page(self):
        return _FakePage()


# --- Tests for proxy-blocking-on-rotation behaviour -----------------------

@pytest.mark.asyncio
async def test_blocked_proxy_not_retried():
    """A proxy that got blocked is skipped on later rotations and pool
    exhaustion raises cleanly (no further browser launch)."""
    from infrastructure.transports.wb_browser import (
        WildberriesBrowserTransport, WildberriesBlockError,
        HTTP498Error,
    )
    from infrastructure.transports.proxy_pool import ProxyPool

    pool = ProxyPool.from_list([
        {"server": "http://a.b:1080"},
        {"server": "http://c.d:2080"},
    ])

    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
    ) as mock_ip:
        created = []

        def fake_ctor(proxy=None, seed=None, humanize=None):
            created.append(proxy)
            return _FakeBrowser()

        mock_ip.return_value = fake_ctor
        calls = [0]

        async def _patched_iter(self, product_url):
            calls[0] += 1
            raise HTTP498Error(f"HTTP 498 (call {calls[0]})")
            yield  # unreachable, keeps the async generator

        with patch.object(
            WildberriesBrowserTransport,
            "_iter_product_and_reviews",
            _patched_iter, create=True,
        ):
            transport = WildberriesBrowserTransport(
                product_url="https://test.example.com",
                proxy_pool=pool,
            )
            with pytest.raises(WildberriesBlockError) as excinfo:
                async for _ in transport.iter_review_batches(
                    "https://test.example.com",
                ):
                    pass

    # Exactly the two distinct proxies are tried; the first one is never
    # retried because it was marked blocked.
    assert [p["server"] for p in created] == [
        "http://a.b:1080", "http://c.d:2080",
    ]
    assert len(created) == 2

    # Both proxies are now marked blocked; the pool is fully exhausted.
    stats = pool.get_stats()
    assert stats["blocked"] == 2
    assert stats["available"] == 0
    assert "exhausted" in str(excinfo.value).lower()


def test_wb_proxy_rotate_max_restarts_flag():
    """--wb-proxy-rotate-max-restarts is parsed; the default is 3."""
    from marketplace_maps_parser.cli_args import parse_args

    args = parse_args([
        "--marketplace", "wildberries",
        "--url", "https://www.wildberries.ru/c",
        "--wb-proxy-rotate-max-restarts", "7",
    ])
    assert args.wb_proxy_rotate_max_restarts == 7

    default_args = parse_args([
        "--marketplace", "wildberries",
        "--url", "https://www.wildberries.ru/c",
    ])
    assert default_args.wb_proxy_rotate_max_restarts == 3


# --- Tests for transient (retryable) vs permanent (burned) blocks --------

def test_wb_error_retryable_mapping():
    """429/5xx are transient (retryable); 498/403 are permanent (burned)."""
    from infrastructure.transports.wb_browser import (
        _wb_error, RetryableHTTPError, HTTP498Error, HTTP403Error,
    )
    e429 = _wb_error(429)
    assert isinstance(e429, RetryableHTTPError)
    assert e429.retryable is True
    e503 = _wb_error(503)
    assert isinstance(e503, RetryableHTTPError)
    assert e503.retryable is True
    e498 = _wb_error(498)
    assert isinstance(e498, HTTP498Error)
    assert e498.retryable is False
    e403 = _wb_error(403)
    assert isinstance(e403, HTTP403Error)
    assert e403.retryable is False


@pytest.mark.asyncio
async def test_transient_error_does_not_burn_proxy():
    """A transient error (429) reuses the same proxy after backoff and
    leaves it un-burned (no rotation to the next proxy)."""
    from infrastructure.transports.wb_browser import (
        WildberriesBrowserTransport, RetryableHTTPError,
    )
    from infrastructure.transports.proxy_pool import ProxyPool

    pool = ProxyPool.from_list([
        {"server": "http://a.b:1080"},
        {"server": "http://c.d:2080"},
    ])

    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
    ) as mock_ip:
        created = []

        def fake_ctor(proxy=None, seed=None, humanize=None):
            created.append(proxy)
            return _FakeBrowser()

        mock_ip.return_value = fake_ctor
        calls = [0]

        async def _patched_iter(self, product_url):
            calls[0] += 1
            if calls[0] == 1:
                raise RetryableHTTPError("HTTP 429")
            return
            yield  # unreachable, keeps the async generator

        with patch.object(
            WildberriesBrowserTransport,
            "_iter_product_and_reviews",
            _patched_iter, create=True,
        ):
            transport = WildberriesBrowserTransport(
                product_url="https://test.example.com",
                proxy_pool=pool,
            )
            async for _ in transport.iter_review_batches(
                "https://test.example.com",
            ):
                pass

    # The proxy was NOT burned (transient error) — nothing in the pool
    # is blocked, and the same proxy is reused (not rotated to the next).
    assert pool.get_stats()["blocked"] == 0
    assert created[1]["server"] == created[0]["server"] == "http://a.b:1080"
