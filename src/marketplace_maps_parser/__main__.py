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
        choices=("playwright", "curl_cffi", "hybrid"),
        default="playwright",
        help=(
            "Ozon only: 'playwright' (default) uses invisible-"
            "playwright to drive a real browser; 'curl_cffi' uses "
            "curl_cffi which mimics the TLS fingerprint of real "
            "Chrome/Firefox — faster, lighter, but cannot solve "
            "Cloudflare JS challenges and does not support "
            "--strategy scroll. 'hybrid' tries curl_cffi first and "
            "falls back to playwright on persistent Cloudflare "
            "challenge — fast when Cloudflare is permissive, "
            "robust when it isn't."
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
    if args.transport == "curl_cffi":
        from infrastructure.transports.curl_cffi import (
            CurlCffiTransport,
        )
        return CurlCffiTransport(
            timeout=args.timeout_ms / 1000.0,
            debug_dir=args.debug_dir,
            impersonate=args.impersonate,
        )

    if args.transport == "hybrid":
        from infrastructure.transports.hybrid import HybridTransport
        return HybridTransport(
            curl_cffi_kwargs={
                "timeout": args.timeout_ms / 1000.0,
                "impersonate": args.impersonate,
            },
            playwright_kwargs={
                "timeout_ms": args.timeout_ms,
                "settle_ms": args.settle_ms,
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
        humanize=not args.no_humanize,
        fetch_strategy=args.fetch_strategy,
        stealth=not args.no_stealth,
    )


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
