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
