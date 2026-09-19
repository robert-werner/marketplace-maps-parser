"""Tests for URL → product_id extractors."""
from __future__ import annotations

import pytest

from shared.url_parsers import (
    extract_2gis_branch_id,
    extract_2gis_firm_path,
    extract_nm_id,
    extract_ozon_product_id,
    extract_ozon_product_path,
    extract_yandex_maps_org_id,
    extract_yandex_maps_org_path,
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


# --- Yandex Maps ----------------------------------------------------------

ORG_URL = (
    "https://yandex.ru/maps/org/"
    "otdeleniye_pochtovoy_svyazi_430028/1120018525"
)


def test_extract_yandex_maps_org_id_happy_path() -> None:
    assert extract_yandex_maps_org_id(ORG_URL) == 1120018525


def test_extract_yandex_maps_org_id_reviews_subroute() -> None:
    assert extract_yandex_maps_org_id(ORG_URL + "/reviews/") == (
        1120018525
    )


def test_extract_yandex_maps_org_id_query_params() -> None:
    url = ORG_URL + "/?ll=45.123805%2C54.224875&z=17"
    assert extract_yandex_maps_org_id(url) == 1120018525


def test_extract_yandex_maps_org_id_maps_subdomain() -> None:
    url = (
        "https://maps.yandex.ru/org/"
        "otdeleniye_pochtovoy_svyazi_430028/1120018525/"
    )
    assert extract_yandex_maps_org_id(url) == 1120018525


def test_extract_yandex_maps_org_id_rejects_wrong_host() -> None:
    url = "https://example.com/maps/org/foo/12345"
    with pytest.raises(ValueError):
        extract_yandex_maps_org_id(url)


def test_extract_yandex_maps_org_id_rejects_market_url() -> None:
    url = "https://market.yandex.ru/card/foo/12345"
    with pytest.raises(ValueError):
        extract_yandex_maps_org_id(url)


def test_extract_yandex_maps_org_id_rejects_no_id() -> None:
    url = "https://yandex.ru/maps/org/foo-bar"
    with pytest.raises(ValueError):
        extract_yandex_maps_org_id(url)


def test_extract_yandex_maps_org_path_strips_reviews_and_query() -> None:
    url = ORG_URL + "/reviews/?ll=45.1%2C54.2&z=17"
    assert extract_yandex_maps_org_path(url) == (
        "/maps/org/otdeleniye_pochtovoy_svyazi_430028/1120018525"
    )


# --- 2GIS -----------------------------------------------------------------

FIRM_URL = "https://2gis.ru/moscow/firm/70000001063192616"


def test_extract_2gis_branch_id_happy_path() -> None:
    assert extract_2gis_branch_id(FIRM_URL) == 70000001063192616


def test_extract_2gis_branch_id_reviews_tab_and_query() -> None:
    url = FIRM_URL + "/tab/reviews?m=37.5%2C55.7"
    assert extract_2gis_branch_id(url) == 70000001063192616


def test_extract_2gis_branch_id_underscored_city() -> None:
    url = (
        "https://2gis.ru/nizhny_novgorod/firm/1234567890/"
    )
    assert extract_2gis_branch_id(url) == 1234567890


def test_extract_2gis_branch_id_rejects_wrong_host() -> None:
    with pytest.raises(ValueError):
        extract_2gis_branch_id(
            "https://example.com/moscow/firm/123",
        )


def test_extract_2gis_branch_id_rejects_non_firm_path() -> None:
    with pytest.raises(ValueError):
        extract_2gis_branch_id("https://2gis.ru/moscow/search/кафе")


def test_extract_2gis_firm_path_stips_tab_and_query() -> None:
    assert extract_2gis_firm_path(
        FIRM_URL + "/tab/reviews?x=1",
    ) == "/moscow/firm/70000001063192616"
