"""Tests for the proxy pool module."""
from __future__ import annotations

import pytest
from pathlib import Path

from infrastructure.transports.proxy_pool import (
    ProxyPool,
    parse_proxy_file,
    parse_proxy_line,
)


# ---------------------------------------------------------------------------
# parse_proxy_line
# ---------------------------------------------------------------------------


def test_parse_proxy_line_host_port():
    """host:port → http://host:port (no auth)"""
    result = parse_proxy_line("1.2.3.4:8080")
    assert result == {"server": "http://1.2.3.4:8080"}


def test_parse_proxy_line_user_pass_host_port():
    """user:pass@host:port → http://host:port with auth"""
    result = parse_proxy_line("user:pass@1.2.3.4:8080")
    assert result == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }


def test_parse_proxy_line_http_scheme():
    """http://user:pass@host:port → parsed correctly"""
    result = parse_proxy_line("http://user:pass@1.2.3.4:8080")
    assert result == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }


def test_parse_proxy_line_socks5():
    """socks5://host:port → socks5 proxy"""
    result = parse_proxy_line("socks5://1.2.3.4:1080")
    assert result == {"server": "socks5://1.2.3.4:1080"}


def test_parse_proxy_line_socks5_with_auth():
    """socks5://user:pass@host:port → socks5 proxy with auth"""
    result = parse_proxy_line("socks5://user:pass@1.2.3.4:1080")
    assert result == {
        "server": "socks5://1.2.3.4:1080",
        "username": "user",
        "password": "pass",
    }


def test_parse_proxy_line_empty_returns_none():
    assert parse_proxy_line("") is None
    assert parse_proxy_line("   ") is None


def test_parse_proxy_line_comment_returns_none():
    assert parse_proxy_line("# this is a comment") is None
    assert parse_proxy_line("  # indented comment") is None


def test_parse_proxy_line_invalid_returns_none():
    """Missing port or hostname → None"""
    assert parse_proxy_line("no-port-here") is None
    assert parse_proxy_line(":8080") is None


# ---------------------------------------------------------------------------
# parse_proxy_file
# ---------------------------------------------------------------------------


def test_parse_proxy_file_reads_all_valid_lines(tmp_path):
    """The file should be read, with comments and blanks ignored."""
    path = tmp_path / "proxies.txt"
    path.write_text(
        "# Comment line\n"
        "1.2.3.4:8080\n"
        "\n"
        "user:pass@5.6.7.8:3128\n"
        "# Another comment\n"
        "socks5://9.10.11.12:1080\n",
        encoding="utf-8",
    )

    proxies = parse_proxy_file(path)
    assert len(proxies) == 3
    assert proxies[0] == {"server": "http://1.2.3.4:8080"}
    assert proxies[1] == {
        "server": "http://5.6.7.8:3128",
        "username": "user",
        "password": "pass",
    }
    assert proxies[2] == {"server": "socks5://9.10.11.12:1080"}


def test_parse_proxy_file_empty_file_raises(tmp_path):
    """An empty file (or all-comments) should raise ValueError."""
    path = tmp_path / "empty.txt"
    path.write_text("# only comments\n\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no valid proxies"):
        parse_proxy_file(path)


def test_parse_proxy_file_missing_file_raises():
    """A non-existent file should raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        parse_proxy_file("/nonexistent/proxies.txt")


# ---------------------------------------------------------------------------
# ProxyPool — round-robin rotation
# ---------------------------------------------------------------------------


def test_proxy_pool_round_robin():
    """next() should cycle through proxies in order."""
    pool = ProxyPool.from_list([
        {"server": "http://1.1.1.1:80"},
        {"server": "http://2.2.2.2:80"},
        {"server": "http://3.3.3.3:80"},
    ])

    # First 3 calls should return 1, 2, 3
    assert pool.next()["server"] == "http://1.1.1.1:80"
    assert pool.next()["server"] == "http://2.2.2.2:80"
    assert pool.next()["server"] == "http://3.3.3.3:80"
    # 4th call wraps back to 1
    assert pool.next()["server"] == "http://1.1.1.1:80"


def test_proxy_pool_single_proxy():
    """A single-proxy pool always returns the same proxy."""
    pool = ProxyPool.single({"server": "http://only:80"})
    assert pool.next()["server"] == "http://only:80"
    assert pool.next()["server"] == "http://only:80"
    assert pool.next()["server"] == "http://only:80"


def test_proxy_pool_mark_blocked():
    """A blocked proxy should be skipped on subsequent next() calls."""
    pool = ProxyPool.from_list([
        {"server": "http://1.1.1.1:80"},
        {"server": "http://2.2.2.2:80"},
        {"server": "http://3.3.3.3:80"},
    ])

    # Block proxy 2
    pool.mark_blocked({"server": "http://2.2.2.2:80"})
    assert pool.available_count == 2

    # next() should skip 2 and cycle through 1 and 3
    results = [pool.next()["server"] for _ in range(6)]
    # 2.2.2.2 should never appear
    assert "http://2.2.2.2:80" not in results
    # Both 1 and 3 should appear
    assert "http://1.1.1.1:80" in results
    assert "http://3.3.3.3:80" in results


def test_proxy_pool_all_blocked_returns_none():
    """When all proxies are blocked, next() returns None."""
    pool = ProxyPool.from_list([
        {"server": "http://1.1.1.1:80"},
    ])
    pool.mark_blocked({"server": "http://1.1.1.1:80"})

    assert pool.next() is None
    assert pool.all_blocked() is True


def test_proxy_pool_reset_blocked():
    """reset_blocked() makes all proxies available again."""
    pool = ProxyPool.from_list([
        {"server": "http://1.1.1.1:80"},
    ])
    pool.mark_blocked({"server": "http://1.1.1.1:80"})
    assert pool.available_count == 0

    pool.reset_blocked()
    assert pool.available_count == 1
    assert pool.next()["server"] == "http://1.1.1.1:80"


def test_proxy_pool_stats():
    """get_stats() returns the current pool state."""
    pool = ProxyPool.from_list([
        {"server": "http://1.1.1.1:80"},
        {"server": "http://2.2.2.2:80"},
    ])
    assert pool.get_stats() == {"total": 2, "available": 2, "blocked": 0}

    pool.mark_blocked({"server": "http://1.1.1.1:80"})
    assert pool.get_stats() == {"total": 2, "available": 1, "blocked": 1}


def test_proxy_pool_empty_raises():
    """An empty proxy list should raise ValueError."""
    with pytest.raises(ValueError):
        ProxyPool.from_list([])


def test_proxy_pool_from_file(tmp_path):
    """from_file() loads proxies from a file."""
    path = tmp_path / "proxies.txt"
    path.write_text(
        "1.2.3.4:8080\n"
        "5.6.7.8:3128\n",
        encoding="utf-8",
    )

    pool = ProxyPool.from_file(path)
    assert pool.size == 2
    assert pool.next()["server"] == "http://1.2.3.4:8080"
    assert pool.next()["server"] == "http://5.6.7.8:3128"
