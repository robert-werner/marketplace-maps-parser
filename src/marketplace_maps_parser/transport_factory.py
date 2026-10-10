"""Lazy transport construction; the CLI does not import browser runtimes."""
from __future__ import annotations

import argparse
from typing import Any


async def _build_ozon_transport(
    args: argparse.Namespace,
) -> Any:
    """Construct the Ozon transport based on --transport.

    Returns an object that implements the OzonBrowserTransport
    Protocol (iter_ozon_reviews_json, iter_ozon_reviews_by_scroll,
    iter_all_ozon_reviews, get_ozon_reviews_json).
    """
    # Build proxy pool / single proxy from CLI args.
    pinned_proxy = getattr(args, "_pinned_proxy", None)
    proxy_pool = (
        None if pinned_proxy is not None else await _build_proxy_pool(args)
    )
    single_proxy = (
        pinned_proxy
        if pinned_proxy is not None
        else _build_single_proxy(args) if proxy_pool is None else None
    )

    cookies = None
    if args.cookies:
        from infrastructure.transports.cookie_loader import (
            load_cookies_file,
        )
        cookies = load_cookies_file(args.cookies)
        print(
            f"Ozon: загружено cookies из {args.cookies}: "
            f"{len(cookies)} шт."
        )

    if args.transport != "playwright":
        raise ValueError(f"Unknown browser transport: {args.transport!r}")
    # API navigation/fetch still runs inside Invisible Playwright.
    from infrastructure.transports.browser_json import (
        BrowserJsonTransport,
    )
    return BrowserJsonTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=args.debug_dir,
        proxy=single_proxy,
        proxy_pool=proxy_pool,
        humanize=not args.no_humanize,
        fetch_strategy=args.fetch_strategy,
        stealth=not args.no_stealth,
        cookies=cookies,
        screenshots=args.screenshots,
        block_assets=not args.no_block_assets,
        debug_dumps=getattr(args, "debug_dumps", False),
        page_delay_seconds=args.page_delay_seconds,
    )


async def _build_proxy_pool(
    args: argparse.Namespace,
) -> Any:
    """Build a proxy pool from --proxy-list or --free-proxy.

    Returns None if neither was provided.

    Priority: --proxy-list > --free-proxy (proxy-list takes
    precedence because residential proxies from a file are more
    reliable than free public proxies).
    """
    if args.proxy_list:
        from infrastructure.transports.proxy_pool import ProxyPool
        return ProxyPool.from_file(args.proxy_list)

    if args.free_proxy:
        from infrastructure.transports.free_proxy_pool import (
            FreeProxyPool,
        )
        country_id = None
        if args.free_proxy_country:
            country_id = [
                c.strip() for c in args.free_proxy_country.split(",")
                if c.strip()
            ]
        print(
            "[info] --free-proxy: загружаю бесплатные публичные "
            "proxy через free-proxy package..."
            + (f" (country={country_id})" if country_id else "")
            + (" (elite)" if args.free_proxy_elite else "")
        )
        # Async factory: the free-proxy batch fetch runs in a
        # worker thread so the event loop is never blocked.
        return await FreeProxyPool.create_async(
            country_id=country_id,
            elite=args.free_proxy_elite,
        )

    return None


def _build_single_proxy(
    args: argparse.Namespace,
) -> dict[str, str] | None:
    """Build a single proxy dict from --proxy. Returns None if no
    single proxy was provided."""
    if not args.proxy:
        return None
    from infrastructure.transports.proxy_pool import parse_proxy_line
    proxy = parse_proxy_line(args.proxy)
    if proxy is None:
        raise SystemExit(
            f"Invalid --proxy format: {args.proxy!r}. "
            "Expected: 'http://host:port' or "
            "'http://user:pass@host:port' or 'socks5://host:port'"
        )
    return proxy
