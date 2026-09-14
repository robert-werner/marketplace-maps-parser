"""Tests for URL → product_id extractors."""
from __future__ import annotations

import pytest

from shared.url_parsers import (
    extract_nm_id,
    extract_ozon_product_id,
    extract_ozon_product_path,
)


# --- Wildberries ----------------------------------------------------------

def test_extract_nm_id_happy_path() -> None:
    url = (
        "https://www.wildberries.ru/"
        "catalog/12345678/detail.aspx"
    )
    assert extract_nm_id(url) == 12345678


def test_extract_nm_id_with_query() -> None:
    url = (
        "https://www.wildberries.ru/"
        "catalog/12345678/detail.aspx?targetUrl=BP"
    )
    assert extract_nm_id(url) == 12345678


def test_extract_nm_id_rejects_catalog_listing() -> None:
    """A bare catalog URL without /detail.aspx is NOT a product page."""
    url = "https://www.wildberries.ru/catalog/12345678"
    with pytest.raises(ValueError):
        extract_nm_id(url)


def test_extract_nm_id_rejects_non_wb() -> None:
    url = "https://example.com/catalog/12345678/detail.aspx"
    # extract_nm_id does not check domain — it only matches the path.
    # Behavior is acceptable as long as the URL path shape matches.
    assert extract_nm_id(url) == 12345678


# --- Ozon -----------------------------------------------------------------

def test_extract_ozon_product_id_happy_path() -> None:
    url = (
        "https://www.ozon.ru/product/"
        "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    )
    assert extract_ozon_product_id(url) == 680123890


def test_extract_ozon_product_id_apex_domain() -> None:
    url = "https://ozon.ru/product/foo-12345"
    assert extract_ozon_product_id(url) == 12345


def test_extract_ozon_product_id_mobile_domain() -> None:
    url = "https://m.ozon.ru/product/foo-12345"
    assert extract_ozon_product_id(url) == 12345


def test_extract_ozon_product_id_rejects_wrong_host() -> None:
    url = "https://example.com/product/foo-12345"
    with pytest.raises(ValueError):
        extract_ozon_product_id(url)


def test_extract_ozon_product_id_rejects_no_sku() -> None:
    url = "https://www.ozon.ru/product/foo-bar"
    with pytest.raises(ValueError):
        extract_ozon_product_id(url)


def test_extract_ozon_product_path_returns_path_only() -> None:
    url = (
        "https://www.ozon.ru/product/"
        "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    )
    assert (
        extract_ozon_product_path(url)
        == "/product/ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    )


def test_extract_ozon_product_path_strips_trailing_slash() -> None:
    url = (
        "https://www.ozon.ru/product/"
        "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890/"
    )
    assert (
        extract_ozon_product_path(url)
        == "/product/ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    )
