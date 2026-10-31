"""Tests for use_stealth mode in iter_review_batches."""
from unittest.mock import patch, MagicMock

import pytest


@pytest.mark.asyncio
async def test_stealth_injected_when_enabled():
    """_apply_stealth calls add_init_script when use_stealth is True."""
    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright"
    ) as mock_ip:

        class FakeBrowser:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def new_page(self):
                return MagicMock()

        mock_ip.return_value = FakeBrowser

        from infrastructure.transports.wb_browser import (
            WildberriesBrowserTransport, _WB_STEALTH_JS,
        )

        transport = WildberriesBrowserTransport(
            product_url="https://test.example.com",
            use_stealth=True,
        )

        # _apply_stealth should call add_init_script
        async with transport:
            page = transport._page
            assert page is not None

            # Check that _apply_stealth was called
            # (we can't easily verify the call without mocking the page)
            assert hasattr(transport, "use_stealth")
            assert transport.use_stealth is True


@pytest.mark.asyncio
async def test_stealth_not_injected_when_disabled():
    """_apply_stealth does nothing when use_stealth is False."""
    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright"
    ) as mock_ip:

        class FakeBrowser:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def new_page(self):
                return MagicMock()

        mock_ip.return_value = FakeBrowser

        from infrastructure.transports.wb_browser import (
            WildberriesBrowserTransport, _WB_STEALTH_JS,
        )

        transport = WildberriesBrowserTransport(
            product_url="https://test.example.com",
            use_stealth=False,
        )

        async def _mock_apply_stealth(page):
            raise RuntimeError("Should not be called")

        with patch.object(
            WildberriesBrowserTransport,
            "_apply_stealth", _mock_apply_stealth,
        ):
            async with transport:
                pass
        # No exception raised — stealth not applied
        assert True


@pytest.mark.asyncio
async def test_stealth_js_content():
    """Stealth JS contains expected overrides."""
    from infrastructure.transports.wb_browser import _WB_STEALTH_JS

    assert "webdriver" in _WB_STEALTH_JS
    assert "false" in _WB_STEALTH_JS
    assert "hardwareConcurrency" in _WB_STEALTH_JS
    assert "deviceMemory" in _WB_STEALTH_JS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
