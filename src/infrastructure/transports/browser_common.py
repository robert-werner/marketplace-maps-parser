# src/infrastructure/transports/browser_common.py
"""Shared pieces of the browser-based Ozon transports.

Extracted from ``browser_json.py`` so every transport (and the
tests) can use them without importing the whole
BrowserJsonTransport module:

- the lazy invisible-playwright factory (GPU-safe);
- the retryable-errors tuple (RuntimeError / TimeoutError /
  Playwright Error when installed);
- the extension-pattern resource blocker (images/fonts/media).

Invisible Playwright owns the fingerprint. No page-level stealth shim
or manual User-Agent overrides are installed here.
"""
from __future__ import annotations

from typing import Any


def import_invisible_playwright() -> type:
    """Lazy import of invisible-playwright, wrapped with
    GPU-safe software-rendering prefs (see gpu_safety.py).

    The library is heavy (patched Playwright + browser binaries)
    and absent in some environments that import this module;
    transports call this inside the async generators that
    actually need a browser.
    """
    from invisible_playwright.async_api import (
        InvisiblePlaywright,
    )

    from infrastructure.transports.gpu_safety import (
        make_gpu_safe,
    )
    return make_gpu_safe(InvisiblePlaywright)

def _get_retryable_errors() -> tuple[type[BaseException], ...]:
    """Return the tuple of exception types that should trigger a retry.

    Built dynamically so we can include ``invisible_playwright``'s
    ``Error`` class only when the library is installed. Always
    includes ``RuntimeError`` and the standard ``TimeoutError`` /
    ``asyncio.TimeoutError`` (in Python 3.11+ these are unified, but
    we keep both for safety on 3.10).
    """
    import asyncio as _asyncio

    types: list[type[BaseException]] = [
        RuntimeError,
        TimeoutError,
        _asyncio.TimeoutError,
    ]
    try:
        from invisible_playwright._pw._impl._errors import (
            Error as PlaywrightError,
        )
        types.append(PlaywrightError)
    except ImportError:
        # invisible-playwright not installed — that's OK, the retry
        # still works on RuntimeError and TimeoutError.
        pass

    return tuple(types)


# Module-level cache so we don't re-import on every retry.
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] | None = None


def _retryable_errors() -> tuple[type[BaseException], ...]:
    global _RETRYABLE_ERRORS
    if _RETRYABLE_ERRORS is None:
        _RETRYABLE_ERRORS = _get_retryable_errors()
    return _RETRYABLE_ERRORS

# Images/fonts/media are the bulk of a reviews page's bytes; review
# photos are never rendered by the scraper — their src urls stay in
# the DOM untouched. resource_type-based routing keeps document /
# script / xhr / stylesheet requests untouched.
BLOCKED_RESOURCE_TYPES = frozenset(
    {"image", "font", "media"}
)

# Route ONLY the asset extensions, not "**/*": every routed request
# detours through this Python process, and routing all ~200 requests
# of a page costs more than the blocked assets save (measured
# 2026-09-16: ~10.5s/page with a catch-all route vs ~8s with
# patterns — see README "Collection speed").
BLOCKED_URL_PATTERNS = (
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.webp",
    "**/*.gif", "**/*.avif", "**/*.woff", "**/*.woff2",
    "**/*.ttf", "**/*.mp4",
)


async def install_resource_blocker(
    page: Any,
    *,
    enabled: bool = True,
) -> None:
    """Abort image/font/media requests on ``page``.

    A no-op when ``enabled`` is False. Pattern routes only — see
    :data:`BLOCKED_URL_PATTERNS` for why a catch-all is slower.
    """
    if not enabled:
        return

    async def _route(route: Any) -> None:
        try:
            resource_type = getattr(
                route.request, "resource_type", "",
            )
            if resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    for pattern in BLOCKED_URL_PATTERNS:
        try:
            # invisible-playwright's Page.route is a coroutine —
            # calling it without await silently drops the route.
            await page.route(pattern, _route)
        except Exception:
            pass
