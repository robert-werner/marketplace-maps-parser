"""The remaining Ozon factory uses Invisible Playwright without stock PW."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from infrastructure.transports.browser_json import BrowserJsonTransport
from marketplace_maps_parser.cli_args import parse_args
from marketplace_maps_parser.parallel_sessions import (
    _child_command,
    _product_child_command,
)
from marketplace_maps_parser.transport_factory import _build_ozon_transport

URL = "https://www.ozon.ru/product/sample-123/"


@pytest.mark.asyncio
async def test_factory_creates_invisible_playwright_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = parse_args([
        "--url", URL, "--debug-dir", str(tmp_path / "debug"),
    ])
    transport = await _build_ozon_transport(args)
    assert type(transport) is BrowserJsonTransport
    assert transport.fetch_strategy == "auto"
    assert not transport.debug_dumps
    assert transport.page_delay_seconds == args.page_delay_seconds

    calls: list[dict[str, Any]] = []
    browser = object()
    closed = False

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        async def __aenter__(self) -> object:
            return browser

        async def __aexit__(self, *args: Any) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        "infrastructure.transports.browser_json._import_invisible_playwright",
        lambda: FakeSession,
    )
    async with transport._browser_context() as session:
        assert session is browser
    assert calls == [{
        "proxy": None, "seed": None, "pin": None, "humanize": True,
    }]
    assert closed


def test_parallel_children_still_use_invisible_playwright(tmp_path: Path):
    args = parse_args([
        "--url", URL, "--strategy", "scroll", "--format", "jsonl",
    ])
    product_cmd = _product_child_command(
        args, URL, tmp_path / "product.jsonl", None,
    )
    range_cmd = _child_command(
        args, tmp_path / "range.jsonl", 1, 2, None,
    )
    for command in (product_cmd, range_cmd):
        child_args = parse_args(command[3:])
        assert child_args.transport == "playwright"
        assert child_args.format == "jsonl"
        assert not any("chromium" in arg for arg in command)
    assert parse_args(product_cmd[3:]).strategy == "scroll"
    assert parse_args(range_cmd[3:]).strategy == "pagination"


def test_children_preserve_serial_streams_and_opt_in_dumps(tmp_path: Path):
    args = parse_args([
        "--url", URL, "--serial-streams", "--debug-dumps",
        "--fetch-strategy", "auto", "--page-delay-seconds", "0.25",
    ])
    command = _product_child_command(
        args, URL, tmp_path / "part.json", None,
    )
    child = parse_args(command[3:])
    assert not child.parallel_streams
    assert child.debug_dumps
    assert child.fetch_strategy == "auto"
    assert child.page_delay_seconds == 0.25
