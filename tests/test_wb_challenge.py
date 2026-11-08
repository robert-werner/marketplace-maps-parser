"""WB browser challenge stage: judge it by the x_wbaas_token cookie."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.transports.wb_browser import (
    HTTP498Error,
    IPReputationError,
    WildberriesBrowserTransport,
)

URL = "https://www.wildberries.ru/catalog/245236068/detail.aspx"


def _page(*, cookies, body):
    page = MagicMock()
    page.goto = AsyncMock(return_value=SimpleNamespace(status=498))
    locator = MagicMock()
    locator.first.wait_for = AsyncMock(side_effect=TimeoutError("no link"))
    page.locator = MagicMock(return_value=locator)
    page.context.cookies = AsyncMock(return_value=cookies)
    page.evaluate = AsyncMock(return_value=body)
    page.url = URL
    return page


async def _run(page):
    t = WildberriesBrowserTransport(product_url=URL, timeout_ms=1000)
    t._page = page
    return [b async for b in t._iter_product_and_reviews(URL)]


async def test_no_token_is_http498():
    with pytest.raises(HTTP498Error):
        await _run(_page(cookies=[], body=""))


async def test_token_then_vpn_refusal_is_ip_reputation():
    page = _page(
        cookies=[{"name": "x_wbaas_token"}],
        body="Возможно, нужно выключить VPN\nНе смогли загрузить страницу",
    )
    with pytest.raises(IPReputationError):
        await _run(page)


async def test_token_without_render_is_session_error_not_block():
    page = _page(cookies=[{"name": "x_wbaas_token"}], body="")
    with pytest.raises(RuntimeError) as info:
        await _run(page)
    assert not isinstance(info.value, (HTTP498Error, IPReputationError))
    assert "did not render" in str(info.value)
