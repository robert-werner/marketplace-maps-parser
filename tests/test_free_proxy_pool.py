"""Tests for the FreeProxyPool class.

These tests stub out the ``fp.fp.FreeProxy`` class so they don't
make real network requests to free proxy sources.
"""
from __future__ import annotations

from typing import Any

import pytest

from infrastructure.transports import free_proxy_pool as fpp_module
from infrastructure.transports.free_proxy_pool import FreeProxyPool


# ---------------------------------------------------------------------------
# Stub FreeProxy
# ---------------------------------------------------------------------------


class _FakeFreeProxy:
    """Stand-in for fp.fp.FreeProxy that returns a fixed list of
    proxy strings."""

    # Class-level list of proxies to return. Tests can modify this
    # before creating a FreeProxyPool.
    _proxy_strings: list[str] = [
        "1.1.1.1:8080",
        "2.2.2.2:3128",
        "3.3.3.3:1080",
    ]

    def __init__(
        self,
        country_id=None,
        timeout=0.5,
        rand=False,
        anonym=False,
        elite=False,
        google=None,
        https=False,
        url="https://www.google.com",
        request_timeout=10,
    ) -> None:
        self._country_id = country_id
        self._elite = elite

    def get_proxy_list(self, repeat=False) -> list[str]:
        return list(self._proxy_strings)

    def get(self, repeat=False) -> str:
        lst = self.get_proxy_list()
        return f"http://{lst[0]}" if lst else ""


def _patch_free_proxy(monkeypatch, proxy_strings: list[str] | None = None):
    """Patch the free-proxy import to use a fake implementation."""
    if proxy_strings is not None:
        _FakeFreeProxy._proxy_strings = proxy_strings

    monkeypatch.setattr(
        fpp_module, "_import_free_proxy", lambda: _FakeFreeProxy,
    )


# ---------------------------------------------------------------------------
# Construction and initial batch
# ---------------------------------------------------------------------------


def test_free_proxy_pool_loads_initial_batch(monkeypatch):
    """FreeProxyPool should fetch proxies on construction."""
    _patch_free_proxy(monkeypatch, ["1.1.1.1:80", "2.2.2.2:80"])

    pool = FreeProxyPool()
    assert pool.size == 2
    assert pool.available_count == 2


def test_free_proxy_pool_converts_to_dict_format(monkeypatch):
    """FreeProxy returns 'host:port' strings — FreeProxyPool
    converts them to invisible-playwright dict format
    ({'server': 'http://host:port'}).
    """
    _patch_free_proxy(monkeypatch, ["1.1.1.1:80"])

    pool = FreeProxyPool()
    proxy = pool.next()
    assert proxy is not None
    assert proxy["server"] == "http://1.1.1.1:80"


def test_free_proxy_pool_round_robin(monkeypatch):
    """next() should cycle through proxies in order."""
    _patch_free_proxy(
        monkeypatch, ["1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"],
    )

    pool = FreeProxyPool()
    assert pool.next()["server"] == "http://1.1.1.1:80"
    assert pool.next()["server"] == "http://2.2.2.2:80"
    assert pool.next()["server"] == "http://3.3.3.3:80"
    # wraps back
    assert pool.next()["server"] == "http://1.1.1.1:80"


# ---------------------------------------------------------------------------
# Mark blocked
# ---------------------------------------------------------------------------


def test_free_proxy_pool_mark_blocked(monkeypatch):
    """Blocked proxies should be skipped."""
    _patch_free_proxy(
        monkeypatch, ["1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"],
    )

    pool = FreeProxyPool()
    pool.mark_blocked({"server": "http://2.2.2.2:80"})
    assert pool.available_count == 2

    results = [pool.next()["server"] for _ in range(6)]
    assert "http://2.2.2.2:80" not in results
    assert "http://1.1.1.1:80" in results
    assert "http://3.3.3.3:80" in results


def test_free_proxy_pool_all_blocked_returns_none(monkeypatch):
    """When all proxies are blocked, next() returns None."""
    _patch_free_proxy(monkeypatch, ["1.1.1.1:80"])

    pool = FreeProxyPool()
    pool.mark_blocked({"server": "http://1.1.1.1:80"})
    # next() should trigger a refill attempt, but the stub
    # returns the same proxy list — which is still blocked.
    assert pool.next() is None
    assert pool.all_blocked() is True


# ---------------------------------------------------------------------------
# Refill — auto-fetch new batch when all blocked
# ---------------------------------------------------------------------------


def test_free_proxy_pool_refill_on_all_blocked(monkeypatch):
    """When all proxies are blocked, next() should auto-refill."""
    _patch_free_proxy(monkeypatch, ["1.1.1.1:80"])

    pool = FreeProxyPool()
    pool.mark_blocked({"server": "http://1.1.1.1:80"})

    # Change the stub to return a new proxy
    _FakeFreeProxy._proxy_strings = ["9.9.9.9:80"]

    # next() should auto-refill and return the new proxy
    proxy = pool.next()
    assert proxy is not None
    assert proxy["server"] == "http://9.9.9.9:80"


def test_free_proxy_pool_reset_blocked(monkeypatch):
    """reset_blocked() should clear the blocked set and refill."""
    _patch_free_proxy(monkeypatch, ["1.1.1.1:80"])

    pool = FreeProxyPool()
    pool.mark_blocked({"server": "http://1.1.1.1:80"})
    assert pool.available_count == 0

    # Change the stub to return fresh proxies
    _FakeFreeProxy._proxy_strings = ["5.5.5.5:80", "6.6.6.6:80"]

    pool.reset_blocked()
    assert pool.available_count == 2
    assert pool.next()["server"] == "http://5.5.5.5:80"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_free_proxy_pool_stats(monkeypatch):
    """get_stats() should return the current pool state."""
    _patch_free_proxy(
        monkeypatch, ["1.1.1.1:80", "2.2.2.2:80"],
    )

    pool = FreeProxyPool()
    assert pool.get_stats() == {
        "total": 2, "available": 2, "blocked": 0,
    }

    pool.mark_blocked({"server": "http://1.1.1.1:80"})
    assert pool.get_stats() == {
        "total": 2, "available": 1, "blocked": 1,
    }


# ---------------------------------------------------------------------------
# Country filter
# ---------------------------------------------------------------------------


def test_free_proxy_pool_passes_country_id(monkeypatch):
    """country_id should be passed to FreeProxy constructor."""
    received_kwargs = {}

    class _CountryTrackingFreeProxy(_FakeFreeProxy):
        def __init__(self, **kwargs):
            received_kwargs.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(
        fpp_module, "_import_free_proxy",
        lambda: _CountryTrackingFreeProxy,
    )

    FreeProxyPool(country_id=["RU"], elite=True)

    assert received_kwargs.get("country_id") == ["RU"]
    assert received_kwargs.get("elite") is True


# ---------------------------------------------------------------------------
# Empty proxy list
# ---------------------------------------------------------------------------


def test_free_proxy_pool_empty_list(monkeypatch):
    """When FreeProxy returns an empty list, next() returns None."""
    _patch_free_proxy(monkeypatch, [])

    pool = FreeProxyPool()
    assert pool.size == 0
    assert pool.next() is None


def test_free_proxy_pool_network_error(monkeypatch):
    """When FreeProxy.get_proxy_list() raises, the pool should
    not crash — it should just have 0 proxies."""
    class _RaisingFreeProxy:
        def __init__(self, **kwargs):
            pass

        def get_proxy_list(self, repeat=False):
            raise ConnectionError("network error")

    monkeypatch.setattr(
        fpp_module, "_import_free_proxy", lambda: _RaisingFreeProxy,
    )

    # Should not raise
    pool = FreeProxyPool()
    assert pool.size == 0
    assert pool.next() is None


# ---------------------------------------------------------------------------
# Dedup within batch
# ---------------------------------------------------------------------------


def test_free_proxy_pool_dedup_within_batch(monkeypatch):
    """If FreeProxy returns duplicate proxies, they should be
    deduplicated within the batch."""
    _patch_free_proxy(
        monkeypatch, ["1.1.1.1:80", "1.1.1.1:80", "2.2.2.2:80"],
    )

    pool = FreeProxyPool()
    assert pool.size == 2  # dedup: 1.1.1.1 appears once
