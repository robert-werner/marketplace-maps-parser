"""WB transport: public API first, browser fallback, strict --wb-api-only."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from infrastructure.transports import wb_public_api
from infrastructure.transports.wb_browser import (
    WildberriesBlockError,
    WildberriesBrowserTransport,
)

URL = "https://www.wildberries.ru/catalog/245236068/detail.aspx"


def _result(window_limited=False):
    return wb_public_api.PublicReviews(
        nm_id=245236068, imt_id=1, title="T", total_count=10, text_count=4,
        average_rating=4.5, returned=2, window_limited=window_limited,
        cards=[{"id": "a", "api": True}, {"id": "b", "api": True}],
    )


def _no_browser():
    def boom(*a, **k):
        raise AssertionError("browser must not start")
    return patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
        side_effect=boom,
    )


async def _collect(transport):
    return [b async for b in transport.iter_review_batches(URL)]


async def test_public_api_skips_browser(monkeypatch):
    async def ok(nm, **kw):
        assert nm == 245236068
        return _result()
    monkeypatch.setattr(wb_public_api, "collect", ok)
    t = WildberriesBrowserTransport(product_url=URL)
    with _no_browser():
        batches = await _collect(t)
    assert [c["id"] for c in batches[0]] == ["a", "b"]
    assert t.collection_path == "public_api"
    assert (t.last_product_title, t.last_total_count) == ("T", 10)
    assert t.last_rating_only_count == 6
    assert t.incomplete_reason is None


async def test_window_limited_sets_incomplete_reason(monkeypatch):
    async def ok(nm, **kw):
        return _result(window_limited=True)
    monkeypatch.setattr(wb_public_api, "collect", ok)
    t = WildberriesBrowserTransport(product_url=URL)
    with _no_browser():
        await _collect(t)
    assert t.incomplete_reason and "newest" in t.incomplete_reason


async def test_api_only_failure_raises_without_browser():
    t = WildberriesBrowserTransport(product_url=URL, use_api_only=True)
    with _no_browser(), pytest.raises(WildberriesBlockError):
        await _collect(t)


async def test_public_api_failure_falls_back_to_browser():
    t = WildberriesBrowserTransport(product_url=URL)
    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
        side_effect=RuntimeError("browser stub"),
    ) as mock_ip, pytest.raises(RuntimeError):
        await _collect(t)
    assert mock_ip.called


async def test_no_browser_api_flag_skips_public_api(monkeypatch):
    async def must_not(nm, **kw):
        raise AssertionError("public API must be skipped")
    monkeypatch.setattr(wb_public_api, "collect", must_not)
    t = WildberriesBrowserTransport(product_url=URL, use_api=False)
    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright",
        side_effect=RuntimeError("browser stub"),
    ), pytest.raises(RuntimeError, match="browser stub"):
        await _collect(t)


def test_stealth_shim_off_by_default():
    assert WildberriesBrowserTransport(product_url=URL).use_stealth is False
