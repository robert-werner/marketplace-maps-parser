"""Proxy pool with rotation for distributing requests across
residential IPs.

Production motivation: Cloudflare blocks requests from datacenter /
VPN / proxy IPs at the network level — before any fingerprint or
stealth check can help. The only reliable way around IP-based
blocking is to use **residential proxies** (IPs from real ISPs) and
to **rotate** them so that a single blocked IP doesn't kill the
whole scraping run.

This module provides:

- ``ProxyPool`` — loads proxies from a file or list, rotates them
  round-robin, and can mark a proxy as "blocked" so it's skipped on
  the next rotation.
- ``parse_proxy_line`` — parses a single proxy line from the
  proxy list file. Supports ``host:port``, ``user:pass@host:port``,
  and ``http://user:pass@host:port`` formats.
- ``parse_proxy_file`` — reads a proxy list file (one proxy per
  line, ``#`` comments allowed) and returns a list of proxy dicts
  in the format invisible-playwright expects
  (``{"server": "...", "username": "...", "password": "..."}``).
"""
from __future__ import annotations

import itertools
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def parse_proxy_line(line: str) -> dict[str, str] | None:
    """Parse a single proxy line into an invisible-playwright proxy
    dict.

    Supported formats:

    - ``host:port`` (no auth)
    - ``user:pass@host:port``
    - ``http://user:pass@host:port``
    - ``socks5://host:port``

    Returns ``None`` if the line is empty or a comment.

    The returned dict has keys ``server``, optionally ``username``
    and ``password``, matching the format invisible-playwright's
    ``proxy`` constructor arg expects.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Add a scheme if missing — urlparse needs one to parse the
    # netloc correctly.
    if "://" not in line:
        if "@" in line:
            # user:pass@host:port — assume http
            line = "http://" + line
        else:
            # host:port — assume http
            line = "http://" + line

    parsed = urlparse(line)
    if not parsed.hostname or not parsed.port:
        return None

    # Determine the scheme (http or socks5)
    scheme = parsed.scheme.lower() if parsed.scheme else "http"
    if scheme not in ("http", "https", "socks5", "socks4"):
        scheme = "http"

    server = f"{scheme}://{parsed.hostname}:{parsed.port}"
    proxy: dict[str, str] = {"server": server}

    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password

    return proxy


def parse_proxy_file(path: str | Path) -> list[dict[str, str]]:
    """Read a proxy list file and return a list of proxy dicts.

    The file format is one proxy per line. Empty lines and lines
    starting with ``#`` are ignored. See ``parse_proxy_line`` for
    the supported per-line formats.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Proxy list file not found: {p}")

    proxies: list[dict[str, str]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            proxy = parse_proxy_line(line)
            if proxy is not None:
                proxies.append(proxy)

    if not proxies:
        raise ValueError(
            f"Proxy list file {p} contains no valid proxies"
        )

    return proxies


class ProxyPool:
    """Round-robin proxy pool with per-proxy block tracking.

    Usage::

        pool = ProxyPool.from_file("proxies.txt")
        proxy = pool.next()  # get the next proxy in rotation
        pool.mark_blocked(proxy)  # mark a proxy as blocked
    """

    def __init__(
        self,
        proxies: list[dict[str, str]],
        *,
        rotation: str = "round_robin",
    ) -> None:
        if not proxies:
            raise ValueError("ProxyPool requires at least one proxy")
        self._proxies = list(proxies)
        self._rotation = rotation
        self._index = 0
        self._blocked: set[str] = set()
        self._lock = threading.Lock()

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        rotation: str = "round_robin",
    ) -> "ProxyPool":
        """Load a proxy pool from a file."""
        return cls(parse_proxy_file(path), rotation=rotation)

    @classmethod
    def from_list(
        cls,
        proxies: list[dict[str, str]],
        *,
        rotation: str = "round_robin",
    ) -> "ProxyPool":
        """Create a proxy pool from a list of proxy dicts."""
        return cls(proxies, rotation=rotation)

    @classmethod
    def single(
        cls,
        proxy: dict[str, str],
    ) -> "ProxyPool":
        """Create a pool with a single proxy (no rotation)."""
        return cls([proxy], rotation="round_robin")

    @property
    def size(self) -> int:
        """Total number of proxies in the pool (including blocked)."""
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        """Number of non-blocked proxies."""
        return len(self._proxies) - len(self._blocked)

    def _proxy_key(self, proxy: dict[str, str]) -> str:
        """A stable string key for a proxy dict (for the blocked set)."""
        return f"{proxy.get('server', '')}|{proxy.get('username', '')}"

    def next(self) -> dict[str, str] | None:
        """Return the next available proxy, or ``None`` if all are
        blocked.

        Rotates round-robin among non-blocked proxies. Thread-safe
        via an internal lock.
        """
        with self._lock:
            if self.available_count == 0:
                return None

            # Try up to len(proxies) times to find a non-blocked one
            for _ in range(len(self._proxies)):
                proxy = self._proxies[self._index]
                self._index = (self._index + 1) % len(self._proxies)
                if self._proxy_key(proxy) not in self._blocked:
                    return proxy

            return None

    def mark_blocked(self, proxy: dict[str, str]) -> None:
        """Mark a proxy as blocked — it won't be returned by
        ``next()`` again.

        Use this when a proxy receives a Cloudflare 403 challenge
        or a "Выключите VPN" block page.
        """
        with self._lock:
            self._blocked.add(self._proxy_key(proxy))

    def reset_blocked(self) -> None:
        """Clear the blocked set — all proxies become available again.

        Useful after a long pause — Cloudflare blocks are often
        temporary (15-60 min) and a previously blocked proxy may
        work again later.
        """
        with self._lock:
            self._blocked.clear()

    def all_blocked(self) -> bool:
        """Return ``True`` if every proxy in the pool is blocked."""
        return self.available_count == 0

    def get_stats(self) -> dict[str, int]:
        """Return a summary of the pool's current state."""
        return {
            "total": self.size,
            "available": self.available_count,
            "blocked": len(self._blocked),
        }
