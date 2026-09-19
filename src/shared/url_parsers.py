# src/shared/url_parsers.py
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

WB_URL_RE = re.compile(
    r"/catalog/(?P<nm_id>\d+)/detail\.aspx/?$",
    re.IGNORECASE,
)

OZON_ID_RE = re.compile(r"-(?P<product_id>\d+)$")

# market.yandex.ru/card/<slug>/<product_id>[/reviews|/spec|...]
# Legacy layout: market.yandex.ru/product/<slug>/<product_id>/...
# Short format: market.yandex.ru/product/<product_id> (no slug)
YANDEX_MARKET_CARD_RE = re.compile(
    r"^/(?:card|product)/(?:(?P<slug>[^/]+)/)?(?P<product_id>\d+)"
    r"(?P<tail>/.*)?$",
)

# yandex.ru/maps/org/<slug>/<org_id>[/reviews]
# maps.yandex.ru mirrors the layout without the /maps prefix.
YANDEX_MAPS_ORG_RE = re.compile(
    r"^/(?:maps/)?org/(?P<slug>[^/]+)/(?P<org_id>\d+)"
    r"(?:/reviews)?/?$",
)

# 2gis.ru/<city>/firm/<branch_id>[/tab/reviews]
TWO_GIS_FIRM_RE = re.compile(
    r"^/[^/]+/firm/(?P<branch_id>\d+)"
    r"(?:/tab/reviews)?/?$",
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


def extract_yandex_maps_org_id(url: str) -> int:
    parsed = urlparse(url)

    if parsed.netloc.lower() not in {
        "yandex.ru",
        "www.yandex.ru",
        "maps.yandex.ru",
    }:
        raise ValueError(f"Некорректный домен Яндекс.Карт: {url}")

    match = YANDEX_MAPS_ORG_RE.match(parsed.path.rstrip("/"))

    if not match:
        raise ValueError(
            f"ID организации не найден в ссылке Яндекс.Карт: {url}"
        )

    return int(match.group("org_id"))


def extract_yandex_maps_org_path(url: str) -> str:
    """/maps/org/<slug>/<org_id> — canonical org path.

    Strips the /reviews sub-route (and any query) so callers can
    append their own sub-routes; always returns the yandex.ru layout
    even for maps.yandex.ru input.
    """
    parsed = urlparse(url)

    extract_yandex_maps_org_id(url)

    match = YANDEX_MAPS_ORG_RE.match(parsed.path.rstrip("/"))

    # Cannot be None: extract_yandex_maps_org_id raises on
    # non-matching URLs (same regex).
    assert match is not None

    return (
        f"/maps/org/{match.group('slug')}/{match.group('org_id')}"
    )


def extract_2gis_branch_id(url: str) -> int:
    parsed = urlparse(url)

    if parsed.netloc.lower() not in {
        "2gis.ru",
        "www.2gis.ru",
    }:
        raise ValueError(f"Некорректный домен 2ГИС: {url}")

    match = TWO_GIS_FIRM_RE.match(parsed.path.rstrip("/"))

    if not match:
        raise ValueError(
            f"ID филиала не найден в ссылке 2ГИС: {url}"
        )

    return int(match.group("branch_id"))


def extract_2gis_firm_path(url: str) -> str:
    """/<city>/firm/<branch_id> — canonical firm path.

    Strips the /tab/reviews sub-route (and any query) so callers can
    append their own sub-routes.
    """
    parsed = urlparse(url)

    extract_2gis_branch_id(url)

    match = TWO_GIS_FIRM_RE.match(parsed.path.rstrip("/"))

    # Cannot be None: extract_2gis_branch_id raises on
    # non-matching URLs (same regex).
    assert match is not None

    city = parsed.path.strip("/").split("/")[0]
    return f"/{city}/firm/{match.group('branch_id')}"


# marketplace name -> (domain roots, the full URL validator).
# The validator is the marketplace's own extractor (domain
# whitelist + path regex), so a look-alike URL of a foreign
# service is never accepted: the host must belong to the source
# AND the path must parse.
_MARKETPLACE_PROBES: tuple[tuple[str, tuple[str, ...], Any], ...] = (
    (
        "yandex",
        ("market.yandex.ru",),
        extract_yandex_market_product_id,
    ),
    (
        "yandex_maps",
        ("yandex.ru", "maps.yandex.ru"),
        extract_yandex_maps_org_id,
    ),
    ("ozon", ("ozon.ru",), extract_ozon_product_id),
    ("2gis", ("2gis.ru",), extract_2gis_branch_id),
    ("wildberries", ("wildberries.ru",), extract_nm_id),
)


def detect_marketplace(url: str) -> str | None:
    """Determine the marketplace from a product/organization URL.

    Returns the CLI name (``"ozon"`` / ``"wildberries"`` /
    ``"yandex"`` / ``"yandex_maps"`` / ``"2gis"``) or ``None``
    when the URL belongs to no known source. ``www.``/``m.``
    subdomains are accepted; the path is validated by the source's
    own extractor, so ``market.yandex.ru`` never misreads as
    Yandex.Maps and vice versa::

        detect_marketplace("https://market.yandex.ru/card/x/1")
        'yandex'
        detect_marketplace("https://yandex.ru/maps/org/x/2/")
        'yandex_maps'
    """
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for name, domains, probe in _MARKETPLACE_PROBES:
        if not any(
            host == domain or host.endswith(f".{domain}")
            for domain in domains
        ):
            continue
        try:
            probe(url)
        except ValueError:
            continue
        return name
    return None