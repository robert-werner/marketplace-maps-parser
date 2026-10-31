"""Tests for use_api_only mode in iter_review_batches."""
from unittest.mock import patch, MagicMock

import pytest


@pytest.mark.asyncio
async def test_api_only_skips_browser():
    """use_api_only should not create any browser context."""
    import httpx

    with patch(
        "infrastructure.transports.wb_browser.import_invisible_playwright"
    ) as mock_ip:
        # If browser was created, import_invisible_playwright would be called
        created = []

        class FakeBrowser:
            async def __aenter__(self):
                created.append(True)
                return self
            async def __aexit__(self, *a):
                pass
            async def new_page(self):
                return MagicMock()

        mock_ip.return_value = FakeBrowser

        # Mock httpx to return a fixed payload
        fake_payload = {
            "data": {
                "feedbacks": [
                    {
                        "id": 1,
                        "nmId": "245236068",
                        "productDetails": {"nmId": "245236068"},
                        "wbUserDetails": {"name": "Test"},
                        "createdDate": "2025-01-01",
                        "productValuation": 5,
                        "text": "Great!",
                    }
                ],
                "nextPage": None,
            }
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_payload
        mock_resp.next_url = None

        with patch("httpx.Client") as mock_client:
            mock_client_instance = MagicMock()
            mock_client_instance.get.return_value = mock_resp
            mock_client.return_value.__enter__.return_value = mock_client_instance
            mock_client_instance.__exit__.side_effect = lambda *a: None

            from infrastructure.transports.wb_browser import (
                WildberriesBrowserTransport,
            )

            transport = WildberriesBrowserTransport(
                product_url="https://www.wildberries.ru/catalog/245236068/detail.aspx",
                use_api_only=True,
            )

            results = []
            async for batch in transport.iter_review_batches(
                "https://www.wildberries.ru/catalog/245236068/detail.aspx",
            ):
                results.append(batch)

            # Browser should not have been created
            assert not created, "Browser was created in API-only mode"
            # Should have 1 review
            assert len(results) >= 1
            assert len(results[0]) == 1


@pytest.mark.asyncio
async def test_api_only_raises_http_error():
    """API-only should raise _wb_error on HTTP >= 400."""
    with patch("httpx.Client") as mock_client:
        import httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 498
        mock_resp.json.return_value = {}

        mock_client_instance = MagicMock()
        mock_client_instance.get.return_value = mock_resp
        mock_client.return_value.__enter__.return_value = mock_client_instance
        mock_client_instance.__exit__.side_effect = lambda *a: None

        from infrastructure.transports.wb_browser import (
            WildberriesBrowserTransport,
            HTTP498Error,
        )

        transport = WildberriesBrowserTransport(
            product_url="https://www.wildberries.ru/catalog/245236068/detail.aspx",
            use_api_only=True,
        )

        with pytest.raises(HTTP498Error):
            async for _ in transport.iter_review_batches(
                "https://www.wildberries.ru/catalog/245236068/detail.aspx",
            ):
                pass
