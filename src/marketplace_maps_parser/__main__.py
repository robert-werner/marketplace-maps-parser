"""Unified CLI entrypoint for marketplace-maps-parser.

Consolidates the four legacy ``main*.py`` scripts into a single argparse-driven
program supporting the Ozon pagination strategy, Ozon DOM-scroll strategy, and
the Wildberries public-API adapter.

Heavy browser transports (``invisible-playwright``) are imported lazily inside
the Ozon branch so the CLI can still run ``--help`` and the Wildberries path
without the browser stack installed.

Usage::

    python -m marketplace_maps_parser \
        --marketplace ozon \
        --url "https://www.ozon.ru/product/..." \
        --output ozon_reviews.jsonl

    python -m marketplace_maps_parser \
        --marketplace ozon \
        --url "https://www.ozon.ru/product/..." \
        --strategy scroll \
        --output ozon_reviews_scroll.jsonl

    python -m marketplace_maps_parser \
        --marketplace wildberries \
        --url "https://www.wildberries.ru/catalog/12345678/detail.aspx" \
        --output wb_reviews.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="marketplace-maps-parser",
        description=(
            "Async review scraper for Ozon, Wildberries, "
            "and Yandex Market (planned)."
        ),
    )
    parser.add_argument(
        "--marketplace",
        choices=("ozon", "wildberries", "yandex"),
        required=True,
        help="Target marketplace.",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Full product URL on the target marketplace.",
    )
    parser.add_argument(
        "--output",
        default="reviews.jsonl",
        help="Output JSONL path (default: reviews.jsonl).",
    )
    parser.add_argument(
        "--strategy",
        choices=("auto", "pagination", "scroll"),
        default="auto",
        help=(
            "Ozon only: 'auto' runs pagination first then scroll as a "
            "fallback / supplement (default, most complete); "
            "'pagination' uses only the internal Ozon API; "
            "'scroll' uses only DOM scroll."
        ),
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Ozon pagination: first page to fetch (default: 1).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=(
            "Ozon pagination: cap number of pages. "
            "Default: unlimited."
        ),
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        help=(
            "Stop after this many unique reviews. "
            "Default: unlimited."
        ),
    )
    parser.add_argument(
        "--debug-dir",
        default="debug_ozon",
        help="Directory for HTML/JSON debug dumps (default: debug_ozon).",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=90_000,
        help="Browser navigation timeout in ms (default: 90000).",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=2_000,
        help="Wait after page load before scraping (default: 2000).",
    )
    parser.add_argument(
        "--no-humanize",
        action="store_true",
        help="Disable invisible-playwright humanize mode.",
    )
    parser.add_argument(
        "--page-delay-seconds",
        type=float,
        default=1.5,
        help=(
            "Jittered delay between pagination page fetches in "
            "seconds (default: 1.5)."
        ),
    )
    parser.add_argument(
        "--scroll-pause-seconds",
        type=float,
        default=1.0,
        help=(
            "Pause between scroll steps in seconds "
            "(default: 1.0)."
        ),
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help=(
            "Per-page retry attempts on transient errors "
            "(HTTP non-200, non-JSON response) with exponential "
            "backoff + jitter (default: 3)."
        ),
    )
    parser.add_argument(
        "--fetch-strategy",
        choices=("navigation", "fetch"),
        default="navigation",
        help=(
            "Ozon only: 'navigation' opens the API URL directly "
            "in the browser tab (default, Cloudflare-friendly); "
            "'fetch' calls fetch() from the page's JS context "
            "(legacy, faster but Cloudflare blocks it more "
            "aggressively)."
        ),
    )
    parser.add_argument(
        "--no-stealth",
        action="store_true",
        help=(
            "Disable stealth init script (default: stealth enabled). "
            "Stealth patches navigator.webdriver, chrome.runtime, "
            "Notification.permission and other signals Cloudflare "
            "uses to detect automated browsers. Disable for "
            "debugging or when stealth causes issues."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("playwright", "curl_cffi", "hybrid", "public_page"),
        default="public_page",
        help=(
            "Ozon only: 'public_page' (default) scrapes the "
            "public review page DOM — least Cloudflare friction, "
            "no internal API; 'playwright' uses invisible-playwright "
            "to drive a real browser hitting the internal API; "
            "'curl_cffi' uses curl_cffi which mimics the TLS "
            "fingerprint of real Chrome/Firefox — faster, lighter, "
            "but cannot solve Cloudflare JS challenges and does not "
            "support --strategy scroll. 'hybrid' tries curl_cffi "
            "first and falls back to playwright on persistent "
            "Cloudflare challenge."
        ),
    )
    parser.add_argument(
        "--impersonate",
        default="chrome120",
        help=(
            "curl_cffi only: which browser TLS fingerprint to "
            "impersonate (default: chrome120). Examples: "
            "chrome120, chrome119, firefox120, safari17_0. See "
            "curl_cffi docs for the full list."
        ),
    )
    parser.add_argument(
        "--randomize-fingerprint",
        action="store_true",
        help=(
            "public_page only: create a fresh "
            "InvisiblePlaywright browser for each page instead of "
            "reusing one. Each new browser gets a new random "
            "fingerprint (seed=None → secrets.randbits(31)), so "
            "every page looks like a different browser to "
            "Cloudflare. Slower (~2-5s browser startup per page) "
            "but maximally stealthy."
        ),
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help=(
            "Single proxy URL for all requests. Format: "
            "'http://host:port' or 'http://user:pass@host:port' "
            "or 'socks5://host:port'. Use a residential proxy to "
            "avoid Cloudflare IP-based blocking."
        ),
    )
    parser.add_argument(
        "--proxy-list",
        default=None,
        help=(
            "Path to a file with proxy URLs (one per line, '#'"
            "comments allowed). Proxies are rotated per page — "
            "each page uses the next proxy in the list. When a "
            "proxy receives a Cloudflare block, it's marked "
            "blocked and skipped on the next rotation. Use "
            "residential proxies for best results."
        ),
    )
    parser.add_argument(
        "--free-proxy",
        action="store_true",
        help=(
            "Automatically fetch free public proxies via the "
            "'free-proxy' PyPI package. Proxies are rotated per "
            "page with auto-refill when all are blocked. "
            "WARNING: free proxies are usually datacenter IPs "
            "(not residential) — Cloudflare may still block "
            "them. For production, use --proxy-list with "
            "residential proxies."
        ),
    )
    parser.add_argument(
        "--free-proxy-country",
        default=None,
        help=(
            "--free-proxy only: filter proxies by country. "
            "Comma-separated ISO country codes, e.g. 'RU' or "
            "'RU,UA,KZ'. Default: any country."
        ),
    )
    parser.add_argument(
        "--free-proxy-elite",
        action="store_true",
        help=(
            "--free-proxy only: only use elite (high-anonymity) "
            "proxies. Default: any anonymity level."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume a previous run: read existing review_ids from "
            "--output and skip them, appending new reviews to the "
            "file instead of overwriting it. Use this when a "
            "previous run was interrupted or when running multiple "
            "times with different --strategy values to fill gaps."
        ),
    )
    return parser.parse_args(argv)


def _review_to_record(review: Any) -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "product_id": review.product.product_id,
        "marketplace": review.product.marketplace,
        "rating": review.rating,
        "text": review.text,
        "author": review.author,
        "created_at": review.created_at,
        "pros": review.pros,
        "cons": review.cons,
        "seller_answer": review.seller_answer,
        "raw": review.raw,
    }


def _load_existing_reviews(
    output: Path,
) -> set[str]:
    """Read ``review_id`` values from an existing JSONL file.

    Used by ``--resume`` to skip reviews already collected in a
    previous run. Returns an empty set if the file does not exist or
    cannot be parsed (so a corrupted file does not block a fresh run).
    """
    if not output.exists():
        return set()

    seen: set[str] = set()
    try:
        with output.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = record.get("review_id")
                if rid:
                    seen.add(str(rid))
    except OSError:
        return set()

    return seen


async def _collect_ozon(args: argparse.Namespace) -> int:
    # Lazy import: heavy transport modules are imported only when
    # the user selects them.
    from infrastructure.marketplaces.ozon import OzonAdapter

    transport = _build_ozon_transport(args)
    adapter = OzonAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # ``--resume``: load review_ids already in the output file so we
    # don't re-emit them. Open in append mode so the new run extends
    # the file rather than overwriting it.
    if args.resume:
        seen_ids = _load_existing_reviews(output)
        if seen_ids:
            print(
                f"Resume: {len(seen_ids)} reviews already in "
                f"{output.name}, will skip them."
            )
        file_mode = "a"
    else:
        seen_ids = set()
        file_mode = "w"

    count = 0

    # When using curl_cffi, scroll strategy is not supported —
    # silently coerce it to pagination to avoid a NotImplementedError
    # at fetch time.
    effective_strategy = args.strategy
    if args.transport == "curl_cffi" and effective_strategy == "scroll":
        print(
            "[info] --transport curl_cffi не поддерживает "
            "--strategy scroll; переключаю на pagination"
        )
        effective_strategy = "pagination"
    elif (
        args.transport == "curl_cffi"
        and effective_strategy == "auto"
    ):
        print(
            "[info] --transport curl_cffi: auto strategy "
            "эквивалентна pagination (scroll не поддерживается)"
        )
        effective_strategy = "pagination"

    try:
        with output.open(file_mode, encoding="utf-8") as file:
            async for review in adapter.iter_all_reviews(
                product_url=args.url,
                strategy=effective_strategy,
                max_reviews=args.max_reviews,
                pagination_max_pages=args.max_pages,
                pagination_start_page=args.start_page,
                page_delay_seconds=args.page_delay_seconds,
                scroll_pause_seconds=args.scroll_pause_seconds,
                retry_attempts=args.retry_attempts,
            ):
                review_id = review.review_id
                if review_id and review_id in seen_ids:
                    continue
                if review_id:
                    seen_ids.add(review_id)

                file.write(
                    json.dumps(
                        _review_to_record(review),
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
                file.flush()
                count += 1

                if count % 100 == 0:
                    print(f"Собрано отзывов: {count}")
    finally:
        # Ensure the transport's HTTP session is closed (curl_cffi
        # holds a connection pool that should be released).
        close = getattr(transport, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    return count


def _build_ozon_transport(args: argparse.Namespace):
    """Construct the Ozon transport based on --transport.

    Returns an object that implements the OzonBrowserTransport
    Protocol (iter_ozon_reviews_json, iter_ozon_reviews_by_scroll,
    iter_all_ozon_reviews, get_ozon_reviews_json).
    """
    # Build proxy pool / single proxy from CLI args.
    proxy_pool = _build_proxy_pool(args)
    single_proxy = _build_single_proxy(args) if proxy_pool is None else None

    if args.transport == "public_page":
        from infrastructure.transports.public_page import (
            PublicPageTransport,
        )
        return PublicPageTransport(
            timeout_ms=args.timeout_ms,
            settle_ms=args.settle_ms,
            debug_dir=args.debug_dir,
            proxy=single_proxy,
            proxy_pool=proxy_pool,
            humanize=not args.no_humanize,
            stealth=not args.no_stealth,
            randomize_fingerprint=args.randomize_fingerprint,
        )

    if args.transport == "curl_cffi":
        from infrastructure.transports.curl_cffi import (
            CurlCffiTransport,
        )
        # curl_cffi takes a proxy URL string, not a dict.
        proxy_url = None
        if single_proxy is not None:
            proxy_url = single_proxy.get("server")
            if single_proxy.get("username"):
                # curl_cffi expects 'http://user:pass@host:port'
                from urllib.parse import urlparse
                p = urlparse(proxy_url)
                proxy_url = (
                    f"{p.scheme}://{single_proxy['username']}:"
                    f"{single_proxy.get('password', '')}@"
                    f"{p.hostname}:{p.port}"
                )
        return CurlCffiTransport(
            timeout=args.timeout_ms / 1000.0,
            debug_dir=args.debug_dir,
            impersonate=args.impersonate,
            proxy=proxy_url,
        )

    if args.transport == "hybrid":
        from infrastructure.transports.hybrid import HybridTransport
        # Hybrid takes playwright proxy dict + curl_cffi proxy URL.
        pw_proxy = single_proxy
        curl_proxy_url = None
        if single_proxy is not None:
            curl_proxy_url = single_proxy.get("server")
            if single_proxy.get("username"):
                from urllib.parse import urlparse
                p = urlparse(curl_proxy_url)
                curl_proxy_url = (
                    f"{p.scheme}://{single_proxy['username']}:"
                    f"{single_proxy.get('password', '')}@"
                    f"{p.hostname}:{p.port}"
                )
        return HybridTransport(
            curl_cffi_kwargs={
                "timeout": args.timeout_ms / 1000.0,
                "impersonate": args.impersonate,
                "proxy": curl_proxy_url,
            },
            playwright_kwargs={
                "timeout_ms": args.timeout_ms,
                "settle_ms": args.settle_ms,
                "proxy": pw_proxy,
                "humanize": not args.no_humanize,
                "fetch_strategy": args.fetch_strategy,
                "stealth": not args.no_stealth,
            },
            debug_dir=args.debug_dir,
        )

    # default: playwright
    from infrastructure.transports.browser_json import (
        BrowserJsonTransport,
    )
    return BrowserJsonTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=args.debug_dir,
        proxy=single_proxy,
        humanize=not args.no_humanize,
        fetch_strategy=args.fetch_strategy,
        stealth=not args.no_stealth,
    )


def _build_proxy_pool(args: argparse.Namespace):
    """Build a proxy pool from --proxy-list or --free-proxy.

    Returns None if neither was provided.

    Priority: --proxy-list > --free-proxy (proxy-list takes
    precedence because residential proxies from a file are more
    reliable than free public proxies).
    """
    if args.proxy_list:
        from infrastructure.transports.proxy_pool import ProxyPool
        return ProxyPool.from_file(args.proxy_list)

    if getattr(args, "free_proxy", False):
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
        return FreeProxyPool(
            country_id=country_id,
            elite=args.free_proxy_elite,
        )

    return None


def _build_single_proxy(args: argparse.Namespace):
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


async def _collect_wildberries(args: argparse.Namespace) -> int:
    # Lazy import: keeps the --help path dependency-light.
    from infrastructure.marketplaces.wildberries import (
        WildberriesAdapter,
    )
    from infrastructure.transports.http import HttpJsonTransport

    async with HttpJsonTransport() as transport:
        adapter = WildberriesAdapter(transport)
        page = await adapter.collect(args.url)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8") as file:
        for review in page.reviews:
            file.write(
                json.dumps(
                    _review_to_record(review),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )

    return len(page.reviews)


async def _run(args: argparse.Namespace) -> int:
    if args.marketplace == "ozon":
        return await _collect_ozon(args)
    if args.marketplace == "wildberries":
        return await _collect_wildberries(args)
    if args.marketplace == "yandex":
        raise SystemExit(
            "Yandex Market adapter is not implemented yet."
        )
    raise SystemExit(f"Unknown marketplace: {args.marketplace}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        count = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130

    print(f"Сбор завершён. Всего отзывов: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
