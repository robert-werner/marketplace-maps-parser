# src/infrastructure/transports/cookie_pool.py
"""Pool of cookie jars (sessions) with round-robin rotation.

Mirror of :mod:`proxy_pool` — but for cookie jars, not proxies.

A *jar* is a logged-in browser session: a list of Playwright-format
cookies (or a Netscape file, normalized via :mod:`cookie_loader`).

Why rotate jars: anti-bot systems (Ozon Variti, Yandex SmartCaptcha
ladder, WB WAF) treat a jar that already saw a captcha as
poisoned — retrying with it on a new IP rarely helps. A logged-in
session is a scarce resource: export working jars (run collection
with ``--save-cookies`` / DevTools / cookie-extension export), drop
them into a directory, and the transport rotates jars between sessions
and restart attempts exactly as it rotates proxies
(``--proxy-list`` / ``--proxy-attempts`` / restart on block).

Usage (CLI):

    marketplace-maps-parser --url <url> --cookies-pool ./jars \
        --cookie-attempts 3

    ``./jars`` — a directory with one jar per ``*.json`` file.

Usage (code):

    pool = CookiePool.from_directory("./jars")
    jar_path = pool.next()             # Path of the next jar, or None
    jar = load_cookies_file(jar_path)
    pool.mark_blocked(jar_path)        # jar failed (block/captcha)

Rotation semantics (like ``ProxyPool``):
* round-robin — ``next()`` always returns the next non-blocked jar;
* a blocked jar is skipped until ``reset_blocked()``;
* ``None`` when the pool is exhausted — the run continues
  without cookies (anonymous), just as a proxyless run does.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from infrastructure.transports.cookie_loader import load_cookies_file

__all__ = ["CookiePool"]


def _jar_key(jar: list[dict[str, Any]]) -> str:
    """A stable string key for a cookie jar (for the blocked set)."""
    payload = sorted(
        (str(c.get("name", "")), str(c.get("value", "")))
        for c in jar
        if isinstance(c, dict)
    )
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False).encode(),
    ).hexdigest()


class CookiePool:
    """Round-robin cookie-jar pool with per-jar block tracking.

    Usage::

        pool = CookiePool.from_directory("./jars")
        jar_path = pool.next()      # Path | None — next jar's file
        jar = pool.next_jar()       # list[dict] | None — next jar
        pool.mark_blocked(jar_path) # jar failed — skip it on rotation
    """

    def __init__(
        self,
        entries: list[tuple[Path | None, list[dict[str, Any]]]],
    ) -> None:
        # Deduplicate identical jars (same content = same identity);
        # keep the first source file.
        seen: set[str] = set()
        unique: list[tuple[Path | None, list[dict[str, Any]]]] = []
        for source, jar in entries:
            key = _jar_key(jar)
            if key not in seen and jar:
                seen.add(key)
                unique.append((source, jar))
        if not unique:
            raise ValueError("CookiePool requires at least one usable jar")
        self._sources: list[Path | None] = [s for s, _ in unique]
        self._cookies: list[list[dict[str, Any]]] = [c for _, c in unique]
        self._blocked: set[str] = set()
        self._index = 0
        self._lock = threading.Lock()

    @classmethod
    def from_directory(
        cls,
        path: str | Path,
        recursive: bool = False,
    ) -> CookiePool:
        """Build a pool from a directory: each ``*.json`` file is one jar.

        Both formats supported (see :mod:`cookie_loader`):
        Playwright/DevTools JSON list and Netscape cookie file.
        Files that don't parse or yield no cookies are skipped.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"CookiePool: directory not found {p}")
        iterator = p.rglob("*.json") if recursive else p.glob("*.json")
        entries: list[tuple[Path | None, list[dict[str, Any]]]] = []
        for f in sorted(iterator):
            try:
                jar = load_cookies_file(f)
            except (OSError, ValueError):
                continue
            if jar:
                entries.append((f, jar))
        return cls.from_entries(entries)

    @classmethod
    def from_entries(
        cls,
        entries: list[tuple[Path | None, list[dict[str, Any]]]],
    ) -> CookiePool:
        """Build a pool from explicit ``(source, jar)`` pairs."""
        return cls(entries)

    @classmethod
    def from_list(
        cls,
        jars: list[list[dict[str, Any]]],
    ) -> CookiePool:
        """In-memory pool (no source files)."""
        return cls.from_entries([(None, jar) for jar in jars])

    @property
    def size(self) -> int:
        """Total number of jars in the pool (including blocked)."""
        return len(self._cookies)

    @property
    def available_count(self) -> int:
        """Number of non-blocked jars."""
        return len(self._cookies) - len(self._blocked)

    def _next_index(self) -> int | None:
        """Advance the rotation pointer, return the index of the next
        non-blocked jar, or ``None`` if all are blocked."""
        with self._lock:
            if self.available_count == 0:
                return None
            for _ in range(len(self._cookies)):
                idx = self._index
                self._index = (self._index + 1) % len(self._cookies)
                if _jar_key(self._cookies[idx]) not in self._blocked:
                    return idx
            return None

    def _current_index(self) -> int | None:
        """Index of the next non-blocked jar without advancing the
        rotation pointer."""
        with self._lock:
            if self.available_count == 0:
                return None
            idx = self._index
            for _ in range(len(self._cookies)):
                if _jar_key(self._cookies[idx]) not in self._blocked:
                    return idx
                idx = (idx + 1) % len(self._cookies)
            return None

    def peek_jar(self) -> list[dict[str, Any]] | None:
        """Next available jar without advancing the pointer. Used to
        learn which jar the next run consumed so a failed run's jar
        can be marked blocked on rotation."""
        idx = self._current_index()
        return self._cookies[idx] if idx is not None else None

    def next(self) -> Path | None:
        """Source file of the next jar, or ``None`` if all are blocked."""
        idx = self._next_index()
        if idx is None:
            return None
        source = self._sources[idx]
        return Path(source) if source is not None else None

    def next_jar(self) -> list[dict[str, Any]] | None:
        """Contents of the next jar, or ``None`` if all are blocked."""
        idx = self._next_index()
        return self._cookies[idx] if idx is not None else None

    async def next_async(self) -> Path | None:
        """Async version of ``next()`` — no I/O, kept for interface
        parity with :class:`FreeProxyPool`."""
        return self.next()

    async def next_jar_async(self) -> list[dict[str, Any]] | None:
        """Async version of ``next_jar()``."""
        return self.next_jar()

    def mark_blocked(
        self,
        jar: str | Path | list[dict[str, Any]],
    ) -> None:
        """Mark a jar as poisoned (captcha / 403) — the next rotation
        skips it until ``reset_blocked()``.

        Accepts either the jar's source file (``str``/``Path``) or the
        jar's contents (list of cookies).
        """
        with self._lock:
            if isinstance(jar, (str, Path)):
                source = str(Path(jar).resolve())
                key = None
                for src, cookies in zip(
                    self._sources, self._cookies, strict=True,
                ):
                    if src is not None and str(src.resolve()) == source:
                        key = _jar_key(cookies)
                        break
                if key is None:
                    return
                self._blocked.add(key)
            else:
                self._blocked.add(_jar_key(jar))

    def reset_blocked(self) -> None:
        """Unblock all jars (new run)."""
        with self._lock:
            self._blocked.clear()

    def all_blocked(self) -> bool:
        """Return ``True`` if every jar in the pool is blocked."""
        return self.available_count == 0

    def get_stats(self) -> dict[str, int]:
        """Return a summary of the pool's current state."""
        return {
            "total": self.size,
            "available": self.available_count,
            "blocked": len(self._blocked),
        }
