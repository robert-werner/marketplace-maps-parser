"""Tests for the logged-in session cookie loader."""
from __future__ import annotations

import json

import pytest

from infrastructure.transports.cookie_loader import (
    load_cookies_file,
)


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_json_playwright_format(tmp_path):
    path = _write(tmp_path, "cookies.json", json.dumps([
        {
            "name": "session_id",
            "value": "abc123",
            "domain": ".ozon.ru",
            "path": "/",
            "expires": 1893456000.0,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
    ]))
    cookies = load_cookies_file(path)
    assert cookies == [{
        "name": "session_id",
        "value": "abc123",
        "domain": ".ozon.ru",
        "path": "/",
        "expires": 1893456000.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }]


def test_json_minimal_fields_and_samesite_normalized(tmp_path):
    path = _write(tmp_path, "minimal.json", json.dumps([
        {"name": "a", "value": "1", "domain": ".ozon.ru"},
        # missing value → dropped
        {"name": "bad", "domain": ".ozon.ru"},
        # cookie-editor extension spelling of the expiry
        {
            "name": "b", "value": "2", "domain": ".ozon.ru",
            "expirationDate": 1893456000,
            "sameSite": "no_restriction",
        },
    ]))
    cookies = load_cookies_file(path)
    assert [c["name"] for c in cookies] == ["a", "b"]
    assert cookies[1]["expires"] == 1893456000
    assert cookies[1]["sameSite"] == "Lax"


def test_netscape_format(tmp_path):
    path = _write(tmp_path, "cookies.txt", "\n".join([
        "# Netscape HTTP Cookie File",
        "# comment",
        ".ozon.ru\tTRUE\t/\tTRUE\t1893456000\tsession_id\tabc123",
        ".ozon.ru\tTRUE\t/\tFALSE\t0\ttmp\tv",  # session cookie
        "garbage line without tabs",
    ]))
    cookies = load_cookies_file(path)
    assert [c["name"] for c in cookies] == ["session_id", "tmp"]
    assert cookies[0]["secure"] is True
    assert cookies[0]["expires"] == 1893456000.0
    assert "expires" not in cookies[1]
    assert cookies[1]["secure"] is False


def test_utf8_bom_json(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps([{"name": "x", "value": "1",
                       "domain": ".ozon.ru"}]).encode()
    )
    assert len(load_cookies_file(path)) == 1


def test_empty_file_raises(tmp_path):
    path = _write(tmp_path, "empty.json", "[]")
    with pytest.raises(ValueError):
        load_cookies_file(path)
