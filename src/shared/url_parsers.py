# src/shared/url_parsers.py
from __future__ import annotations

import re
from urllib.parse import urlparse


WB_URL_RE = re.compile(
    r"/catalog/(?P<nm_id>\d+)/detail\.aspx/?$",
    re.IGNORECASE,
)

OZON_ID_RE = re.compile(r"-(?P<product_id>\d+)$")


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