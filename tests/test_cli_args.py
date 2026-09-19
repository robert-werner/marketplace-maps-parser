"""Tests for CLI argument parsing: --marketplace auto-detection."""
from __future__ import annotations

import pytest

from marketplace_maps_parser.cli_args import parse_args


def test_marketplace_detected_from_url() -> None:
    args = parse_args(
        ["--url", "https://market.yandex.ru/card/x/12345678"],
    )
    assert args.marketplace == "yandex"


def test_detection_covers_all_sources() -> None:
    for url, name in (
        (
            "https://yandex.ru/maps/org/firm/1120018525/",
            "yandex_maps",
        ),
        (
            "https://www.ozon.ru/product/telefon-1234567890/",
            "ozon",
        ),
        (
            "https://2gis.ru/moscow/firm/70000001063192616",
            "2gis",
        ),
        (
            "https://www.wildberries.ru/catalog/831948063"
            "/detail.aspx",
            "wildberries",
        ),
    ):
        assert parse_args(["--url", url]).marketplace == name


def test_explicit_marketplace_wins_over_detection() -> None:
    args = parse_args(
        [
            "--marketplace", "ozon",
            "--url", "https://www.ozon.ru/product/x-1/",
        ],
    )
    assert args.marketplace == "ozon"


def test_unknown_url_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--url", "https://example.com/product/1"])


def test_products_file_requires_explicit_marketplace() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--products-file", "products.txt"])
