"""Tests for WildberriesBrowserTransport error classes and init."""
import pytest

from infrastructure.transports.wb_browser import (
    WildberriesBrowserTransport,
    WildberriesBlockError,
    HTTP498Error,
    HTTP403Error,
    CaptchaError,
    _wb_error,
)


class TestErrorClasses:
    def test_wildberries_block_error_is_runtime_error(self):
        assert issubclass(WildberriesBlockError, RuntimeError)

    def test_http498_error_is_wildberries_block_error(self):
        assert issubclass(HTTP498Error, WildberriesBlockError)

    def test_http403_error_is_wildberries_block_error(self):
        assert issubclass(HTTP403Error, WildberriesBlockError)

    def test_captcha_error_is_wildberries_block_error(self):
        assert issubclass(CaptchaError, WildberriesBlockError)

    def test_498_error_message(self):
        err = _wb_error(498)
        assert isinstance(err, HTTP498Error)
        assert "HTTP 498" in str(err)

    def test_403_error_message(self):
        err = _wb_error(403)
        assert isinstance(err, HTTP403Error)
        assert "HTTP 403" in str(err)

    def test_other_error_is_base_block_error(self):
        err = _wb_error(503)
        assert isinstance(err, WildberriesBlockError)
        assert not isinstance(err, HTTP498Error)
        assert not isinstance(err, HTTP403Error)
        assert "HTTP 503" in str(err)

    def test_block_error_catchable_as_runtime_error(self):
        for cls in [HTTP498Error, HTTP403Error, CaptchaError]:
            with pytest.raises(RuntimeError):
                raise cls("test")


class TestInit:
    def test_default_proxy_pool_is_none(self):
        transport = WildberriesBrowserTransport(product_url="https://example.com")
        assert transport.proxy_pool is None
        assert transport.proxy_rotate_max_restarts == 3
        assert transport.use_api_only is False

    def test_custom_proxy_pool(self):
        transport = WildberriesBrowserTransport(
            product_url="https://example.com",
            proxy_pool=None,  # just test the attr
            proxy_rotate_max_restarts=5,
        )
        assert transport.proxy_rotate_max_restarts == 5

    def test_use_api_only(self):
        transport = WildberriesBrowserTransport(
            product_url="https://example.com",
            use_api_only=True,
        )
        assert transport.use_api_only is True

    def test_proxy_stored_in_private_attr(self):
        proxy = {"server": "http://1.2.3.4:1080"}
        transport = WildberriesBrowserTransport(
            product_url="https://example.com",
            proxy=proxy,
        )
        assert transport._proxy == proxy
        assert transport.proxy == proxy
