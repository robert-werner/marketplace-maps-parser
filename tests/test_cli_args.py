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


def test_invisible_playwright_is_the_default_and_explicit_transport() -> None:
    argv = ["--url", "https://www.ozon.ru/product/sample-123/"]
    assert parse_args(argv).transport == "playwright"
    assert parse_args(argv + ["--transport", "playwright"]).transport == (
        "playwright"
    )


@pytest.mark.parametrize("removed_options", [
    ["--transport", "chromium"],
    ["--chromium-headed"],
    ["--chromium-executable", "/unused/chromium"],
])
def test_removed_chromium_options_are_rejected(
    removed_options: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args([
            "--url", "https://www.ozon.ru/product/sample-123/",
            *removed_options,
        ])
    assert exc.value.code == 2


def test_env_var_proxy_is_used_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_PROXY", "socks5://user:pass@host:10000")
    args = parse_args(["--url", "https://www.ozon.ru/product/sample-123/"])
    assert args.proxy == "socks5://user:pass@host:10000"


def test_env_var_cookies_and_save_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_COOKIES", "/path/cookies.json")
    monkeypatch.setenv("MMP_SAVE_COOKIES", "/path/saved.json")
    args = parse_args([
        "--marketplace", "yandex",
        "--url", "https://market.yandex.ru/card/x/12345678",
    ])
    assert args.cookies == "/path/cookies.json"
    assert args.save_cookies == "/path/saved.json"


def test_env_var_cookies_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_COOKIES_POOL", "/path/jars")
    args = parse_args([
        "--marketplace", "yandex",
        "--url", "https://market.yandex.ru/card/x/12345678",
    ])
    assert args.cookies_pool == "/path/jars"


def test_env_var_ints_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_PROXY_ATTEMPTS", "7")
    monkeypatch.setenv("MMP_COOKIE_ATTEMPTS", "5")
    monkeypatch.setenv("MMP_COOKIE_ROTATE_MAX_RESTARTS", "4")
    args = parse_args(["--url", "https://www.ozon.ru/product/sample-123/"])
    assert args.proxy_attempts == 7
    assert args.cookie_attempts == 5
    assert args.cookie_rotate_max_restarts == 4


def test_env_var_bool_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_FREE_PROXY", "true")
    monkeypatch.setenv("MMP_FREE_PROXY_ELITE", "yes")
    args = parse_args([
        "--marketplace", "ozon",
        "--url", "https://www.ozon.ru/product/sample-123/",
    ])
    assert args.free_proxy is True
    assert args.free_proxy_elite is True


def test_env_bool_off_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_FREE_PROXY", "0")
    monkeypatch.setenv("MMP_FREE_PROXY_ELITE", "no")
    args = parse_args([
        "--marketplace", "ozon",
        "--url", "https://www.ozon.ru/product/sample-123/",
    ])
    assert args.free_proxy is False
    assert args.free_proxy_elite is False


def test_cli_overrides_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_PROXY", "socks5://env@host:10000")
    monkeypatch.setenv("MMP_PROXY_ATTEMPTS", "5")
    monkeypatch.setenv("MMP_FREE_PROXY", "true")
    args = parse_args([
        "--url", "https://www.ozon.ru/product/sample-123/",
        "--proxy", "http://cli@host:9999",
        "--proxy-attempts", "9",
        "--free-proxy",
    ])
    assert args.proxy == "http://cli@host:9999"
    assert args.proxy_attempts == 9
    assert args.free_proxy is True


def test_invalid_env_int_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MMP_PROXY_ATTEMPTS", "notanint")
    args = parse_args(["--url", "https://www.ozon.ru/product/sample-123/"])
    assert args.proxy_attempts == 3
