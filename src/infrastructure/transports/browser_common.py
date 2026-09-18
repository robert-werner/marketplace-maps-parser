# src/infrastructure/transports/browser_common.py
"""Shared pieces of the browser-based Ozon transports.

Extracted from ``browser_json.py`` so every transport (and the
tests) can use them without importing the whole
BrowserJsonTransport module:

- the lazy invisible-playwright factory (GPU-safe);
- the retryable-errors tuple (RuntimeError / TimeoutError /
  Playwright Error when installed);
- the stealth init script that patches the common Cloudflare
  headless-detection signals;
- the extension-pattern resource blocker (images/fonts/media).
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

# Stealth init script — patches the most common signals Cloudflare
# uses to detect automated / headless browsers. Adapted from the
# open-source playwright-stealth project (https://github.com/
# Mattwmaster58/playwright_stealth) and tailored for Firefox.
#
# Applied to every fresh page via ``page.add_init_script`` so the
# patches run before any page JS executes.
_STEALTH_INIT_SCRIPT = """
// Hide that we're a WebDriver-controlled browser.
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// Pretend we have the Chrome runtime object that real Chrome
// browsers expose. Some bot detection scripts check for its
// presence.
if (!window.chrome) {
    window.chrome = {
        runtime: {},
        app: {},
        csi: () => {},
        loadTimes: () => {},
    };
}

// Override Notification.permission so it doesn't say "denied" —
// real browsers say "default" until the user has interacted.
if (window.Notification) {
    Object.defineProperty(Notification, 'permission', {
        get: () => 'default',
        configurable: true,
    });
}

// Pretend we have plugins (real browsers have at least PDF viewer).
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {
            name: 'PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
        },
        {
            name: 'Chrome PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
        },
    ],
    configurable: true,
});

// Pretend we have a non-zero set of mime types.
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => [
        {
            type: 'application/pdf',
            suffixes: 'pdf',
            description: 'Portable Document Format',
        },
        {
            type: 'text/pdf',
            suffixes: 'pdf',
            description: 'Portable Document Format',
        },
    ],
    configurable: true,
});

// Make the navigator.languages look real.
Object.defineProperty(navigator, 'languages', {
    get: () => ['ru', 'ru-RU', 'en-US', 'en'],
    configurable: true,
});

// Patch permissions query so it doesn't say "denied" for
// notifications.
const originalQuery = window.navigator.permissions
    ? window.navigator.permissions.query
    : null;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({state: 'default'})
            : originalQuery(parameters)
    );
}

// Make window.outerWidth / outerHeight look non-zero (headless
// browsers often report 0).
if (window.outerWidth === 0 || window.outerHeight === 0) {
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth || 1280,
        configurable: true,
    });
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 85 || 720,
        configurable: true,
    });
}

// Webdriver test: some detection scripts check
// ``window.navigator.webdriver === false`` explicitly. Set it.
try {
    delete Object.getPrototypeOf(navigator).webdriver;
} catch (e) {
    // Some builds don't allow delete on the prototype.
}

// Override the document title to hide the JUGGLER session
// identifier that invisible-playwright's patched Firefox sets as
// the initial window/tab title ("JUGGLER <uuid>"). The JUGGLER
// title is a local UI element (not sent to servers), but it looks
// suspicious on screenshots and to anyone watching the browser.
// We set a neutral title immediately, before any page content
// loads.
try {
    Object.defineProperty(document, 'title', {
        get: () => document.querySelector('title')?.textContent || '',
        set: (value) => {
            let titleEl = document.querySelector('title');
            if (!titleEl) {
                titleEl = document.createElement('title');
                document.head
                    ? document.head.appendChild(titleEl)
                    : null;
            }
            titleEl.textContent = value;
        },
        configurable: true,
    });
    // Set a neutral initial title for the blank page
    if (!document.title || document.title.startsWith('JUGGLER')) {
        document.title = '';
    }
} catch (e) {
    // If we can't override, just blank it out
    try { document.title = ''; } catch (e2) {}
}
"""


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
