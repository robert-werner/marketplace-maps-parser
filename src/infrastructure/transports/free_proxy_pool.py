"""Free proxy pool backed by the ``free-proxy`` PyPI package.

Wraps ``fp.fp.FreeProxy`` to automatically fetch free public proxy
servers and rotate them with the same interface as
``ProxyPool`` (``next()``, ``mark_blocked()``, ``get_stats()``).

⚠️  **WARNING**: Free public proxies are:
  - Usually **datacenter IPs** (not residential) — Cloudflare may
    still block them with "Выключите VPN" pages.
  - **Unreliable** — high failure rate, proxies go offline
    frequently.
  - **Slow** — high latency, limited bandwidth.
  - Often **not HTTPS-capable** — may fail on HTTPS URLs.

  For production use, prefer **residential proxy providers** (Bright
    Data, Smartproxy, Oxylabs, IPRoyal) via ``--proxy-list``.
  Use ``--free-proxy`` for development / testing / quick
    prototyping when you don't have a proxy list handy.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any


def _import_free_proxy():
    """Lazy import of the free-proxy package."""
    from fp.fp import FreeProxy
    return FreeProxy


class FreeProxyPool:
    """Proxy pool backed by the ``free-proxy`` package.

    Implements the same interface as ``ProxyPool`` (``next()``,
    ``mark_blocked()``, ``get_stats()``, ``all_blocked()``,
    ``reset_blocked()``) so it can be used as a drop-in replacement
    in any transport that accepts a ``proxy_pool`` argument.

    On construction, fetches a batch of free proxies via
    ``FreeProxy.get_proxy_list()``. When all fetched proxies are
    blocked, automatically fetches a new batch (``refill()``).

    **Russian proxy priority**: Ozon is a Russian marketplace —
    Russian IPs are least likely to be blocked by Cloudflare. When
    ``country_id`` is ``None`` (default), the pool automatically
    uses ``['RU']`` as the primary country. If RU proxies run out,
    it falls back to neighboring CIS countries (Belarus, Ukraine,
    Kazakhstan) before trying all countries.

    Constructor options mirror ``FreeProxy.__init__``:
    ``country_id``, ``timeout``, ``elite``, ``https``, etc.
    """

    # CIS countries that are geographically close to Russia and
    # may also work for Ozon. Used as fallback when RU proxies run
    # out.
    _CIS_FALLBACK_COUNTRIES = ["BY", "UA", "KZ"]

    def __init__(
        self,
        *,
        country_id: list[str] | None = None,
        timeout: float = 0.5,
        elite: bool = False,
        https: bool = False,
        batch_size: int = 100,
        fetch_on_init: bool = True,
    ) -> None:
        # Default to Russian proxies when no country specified —
        # Ozon is a Russian marketplace and RU IPs are least likely
        # to be blocked by Cloudflare.
        if country_id is None:
            country_id = ["RU"]
        self._country_id = country_id
        self._timeout = timeout
        self._elite = elite
        self._https = https
        self._batch_size = batch_size
        self._proxies: list[dict[str, str]] = []
        self._index = 0
        self._blocked: set[str] = set()
        self._lock = threading.Lock()
        # Track which country sets we've already tried (for
        # fallback logic in _refill_sync).
        self._refill_round = 0
        # Fetch the initial batch. Pass ``fetch_on_init=False`` (or
        # use the async factory ``create_async``) to skip the
        # blocking fetch at construction time.
        if fetch_on_init:
            self._refill_sync()

    @classmethod
    async def create_async(
        cls,
        **kwargs: Any,
    ) -> FreeProxyPool:
        """Async factory: constructs the pool and fetches the first
        proxy batch in a worker thread, so the event loop is never
        blocked by the (network-bound) free-proxy fetch."""
        pool = cls(fetch_on_init=False, **kwargs)
        await asyncio.to_thread(pool.refill)
        return pool

    def _refill_sync(self) -> None:
        """Fetch a new batch of free proxies (synchronous, called
        from __init__ and from _refill under the lock).

        Fallback logic for Russian proxy rotation:

        - Round 0: fetch from the user-specified country (default
          ``['RU']``).
        - Round 1: if RU proxies are exhausted, fetch from CIS
          fallback countries (Belarus, Ukraine, Kazakhstan).
        - Round 2+: fetch from all countries (no country filter).

        Each refill increments ``_refill_round``, so the pool
        progressively widens its search when RU proxies run out.
        """
        FreeProxy = _import_free_proxy()

        # Determine which countries to fetch from based on the
        # current refill round.
        countries_to_try = self._get_countries_for_round(
            self._refill_round,
        )
        self._refill_round += 1

        all_raw: list[str] = []
        for countries in countries_to_try:
            fp = FreeProxy(
                country_id=countries,
                timeout=self._timeout,
                elite=self._elite,
                https=self._https,
            )
            try:
                raw_list = fp.get_proxy_list(repeat=False)
            except Exception as exc:
                print(
                    f"FreeProxyPool: не удалось получить proxy "
                    f"для стран {countries}: {exc}"
                )
                continue

            if raw_list:
                print(
                    f"FreeProxyPool: получено {len(raw_list)} proxy "
                    f"для стран {countries}"
                )
                all_raw.extend(raw_list)
                # If we got enough proxies from this country set,
                # don't try the next fallback.
                if len(all_raw) >= 10:
                    break

        new_proxies: list[dict[str, str]] = []
        for raw in all_raw:
            # FreeProxy returns "host:port" (no scheme). We add
            # "http://" prefix since free proxies rarely support
            # HTTPS tunneling.
            if "://" not in raw:
                raw = f"http://{raw}"
            # Parse via the shared parse_proxy_line helper
            from infrastructure.transports.proxy_pool import (
                parse_proxy_line,
            )
            proxy = parse_proxy_line(raw)
            if proxy is not None:
                # Skip proxies we already have (dedup within batch)
                key = self._proxy_key(proxy)
                if key not in self._blocked and proxy not in new_proxies:
                    new_proxies.append(proxy)

        self._proxies = new_proxies
        self._index = 0
        # Don't clear blocked set — blocked proxies from the
        # previous batch might reappear in the new batch. Keep
        # them blocked so we don't retry a known-bad proxy.
        print(
            f"FreeProxyPool: загружено {len(new_proxies)} proxy "
            f"(round {self._refill_round - 1})"
        )

    def _get_countries_for_round(
        self,
        round_num: int,
    ) -> list[list[str]]:
        """Return the list of country sets to try for a given
        refill round.

        Round 0: user-specified country (default ``['RU']``).
        Round 1: CIS fallback countries (BY, UA, KZ).
        Round 2+: all countries (``None`` — no filter).

        Returns a list of country lists to try in order. The
        ``_refill_sync`` method iterates this list and stops as
        soon as it gets enough proxies from a set.
        """
        if round_num == 0:
            return [self._country_id]
        elif round_num == 1:
            # Fallback to CIS countries if RU proxies ran out.
            # Only do this if the user didn't explicitly specify
            # a non-RU country.
            if self._country_id == ["RU"]:
                return [self._CIS_FALLBACK_COUNTRIES]
            return [self._country_id]
        else:
            # Last resort: all countries, no filter.
            return [None]

    def refill(self) -> None:
        """Fetch a new batch of free proxies (async-safe).

        Called automatically by ``next()`` when all current
        proxies are blocked.
        """
        with self._lock:
            self._refill_sync()

    def _proxy_key(self, proxy: dict[str, str]) -> str:
        """Stable string key for a proxy dict."""
        return f"{proxy.get('server', '')}|{proxy.get('username', '')}"

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        """Count proxies in the current batch that are NOT blocked.

        Note: ``len(self._blocked)`` may include proxies from
        previous batches that are no longer in ``self._proxies``
        (after a refill). So we count by iterating the current list.
        """
        return sum(
            1
            for p in self._proxies
            if self._proxy_key(p) not in self._blocked
        )

    def _next_no_refill(self) -> dict[str, str] | None:
        """Return the next available proxy WITHOUT touching the
        network. ``None`` when no unblocked proxy remains in the
        current batch."""
        with self._lock:
            if self.available_count == 0:
                return None

            for _ in range(len(self._proxies)):
                if self._index >= len(self._proxies):
                    self._index = 0
                proxy = self._proxies[self._index]
                self._index += 1
                if self._proxy_key(proxy) not in self._blocked:
                    return proxy

            return None

    def next(self) -> dict[str, str] | None:
        """Return the next available proxy, or ``None`` if all
        are blocked (even after a refill attempt).

        ⚠️ Synchronous: a refill fetches a new proxy batch over the
        network and blocks the caller. From async code prefer
        ``next_async()``.
        """
        proxy = self._next_no_refill()
        if proxy is not None:
            return proxy

        with self._lock:
            self._refill_sync()

        return self._next_no_refill()

    async def next_async(self) -> dict[str, str] | None:
        """Async version of ``next()``.

        The potentially slow part — fetching a fresh proxy batch
        when the current one is exhausted — runs in a worker thread
        via ``asyncio.to_thread`` so the event loop never blocks.
        """
        proxy = self._next_no_refill()
        if proxy is not None:
            return proxy

        await asyncio.to_thread(self.refill)

        return self._next_no_refill()

    def mark_blocked(self, proxy: dict[str, str]) -> None:
        """Mark a proxy as blocked."""
        with self._lock:
            self._blocked.add(self._proxy_key(proxy))

    def reset_blocked(self) -> None:
        """Clear the blocked set and restart from RU proxies."""
        with self._lock:
            self._blocked.clear()
            self._refill_round = 0  # restart from RU
            self._refill_sync()

    def all_blocked(self) -> bool:
        return self.available_count == 0

    def get_stats(self) -> dict[str, int]:
        return {
            "total": self.size,
            "available": self.available_count,
            "blocked": len(self._blocked),
        }
