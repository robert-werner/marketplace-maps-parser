# src/shared/url_parsers.py
from __future__ import annotations

import re
from urllib.parse import urlparse

WB_URL_RE = re.compile(
    r"/catalog/(?P<nm_id>\d+)/detail\.aspx/?$",
    re.IGNORECASE,
)

OZON_ID_RE = re.compile(r"-(?P<product_id>\d+)$")

# market.yandex.ru/card/<slug>/<product_id>[/reviews|/spec|...]
# Legacy layout: market.yandex.ru/product/<slug>/<product_id>/...
YANDEX_MARKET_CARD_RE = re.compile(
    r"^/(?:card|product)/(?P<slug>[^/]+)/(?P<product_id>\d+)"
    r"(?P<tail>/.*)?$",
)


def extract_nm_id(url: str) -> int:
    parsed = urlparse(url)
    match = WB_URL_RE.search(parsed.path.rstrip("/"))

    if not match:
        raise ValueError(f"Некорректная ссылка Wildberries: {url}")

    return int(match.group("nm_id"))


def extract_ozon_product_id(url: str) -> int:
    parsed = urlparse(url)

    if parsed.netloc.lower() not in {
        "ozon.ru",
        "www.ozon.ru",
        "m.ozon.ru",
    }:
        raise ValueError(f"Некорректный домен Ozon: {url}")

    match = OZON_ID_RE.search(parsed.path.rstrip("/"))

    if not match:
        raise ValueError(f"SKU не найден в ссылке Ozon: {url}")

    return int(match.group("product_id"))


def extract_ozon_product_path(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    extract_ozon_product_id(url)

    if not path.startswith("/product/"):
        raise ValueError(f"Это не ссылка на товар Ozon: {url}")

    return path


def extract_yandex_market_product_id(url: str) -> int:
    parsed = urlparse(url)

    if parsed.netloc.lower() not in {
        "market.yandex.ru",
        "www.market.yandex.ru",
    }:
        raise ValueError(
            f"Некорректный домен Яндекс.Маркета: {url}"
        )

    match = YANDEX_MARKET_CARD_RE.match(parsed.path.rstrip("/"))

    if not match:
        raise ValueError(
            f"ID товара не найден в ссылке Яндекс.Маркета: {url}"
        )

    return int(match.group("product_id"))


def extract_yandex_market_card_path(url: str) -> str:
    """/card/<slug>/<id> — canonical card path.

    Strips any trailing sub-route (/reviews, /spec, …) so callers can
    append their own sub-routes.
    """
    parsed = urlparse(url)

    extract_yandex_market_product_id(url)

    match = YANDEX_MARKET_CARD_RE.match(parsed.path.rstrip("/"))

    # Cannot be None: extract_yandex_market_product_id raises on
    # non-matching URLs (same regex).
    assert match is not None

    return f"/card/{match.group('slug')}/{match.group('product_id')}"