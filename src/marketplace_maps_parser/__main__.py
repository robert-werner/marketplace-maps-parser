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
        default=None,
        help=(
            "Full product URL on the target marketplace "
            "(or use --products-file to collect many products)."
        ),
    )
    parser.add_argument(
        "--products-file",
        default=None,
        help=(
            "Text file with product URLs (one per line, '#'"
            "comments allowed): collect reviews for MANY products"
            "in parallel — one child process per product, at most"
            "--products-sessions running at a time, one proxy from"
            "--proxy-list per product. Ozon only. Parts merge into"
            "--output with review_id dedup (review ids are unique"
            "across products, so a combined file is safe)."
            "Mutually exclusive with --url."
        ),
    )
    parser.add_argument(
        "--products-sessions",
        type=int,
        default=3,
        help=(
            "--products-file only: maximum child processes running"
            "concurrently (default: 3). Each child uses its own"
            "browser and its own proxy — this is the reliable"
            "wall-time multiplier (measured: tabs of one session"
            "serialize, independent processes scale linearly)."
        ),
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
        default=None,
        help=(
            "Directory for HTML/JSON debug dumps. Default: "
            "debug_ozon (ozon), debug_yandex (yandex)."
        ),
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
            "DEFAULT: Russian (RU) proxies first, with fallback "
            "to CIS countries (BY, UA, KZ) and then all "
            "countries. Use --free-proxy-country to override. "
            "WARNING: free proxies are usually datacenter IPs "
            "(not residential) — Cloudflare may still block "
            "them. For production, use --proxy-list with "
            "residential proxies."
        ),
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help=(
            "Path to cookies of a LOGGED-IN Ozon session, injected "
            "into every browser page. Unlocks the full review list "
            "— anonymous sessions cap at ~33 review pages (~990 "
            "reviews). Formats: Playwright/DevTools JSON list "
            "(export with any cookie-editor extension) or Netscape "
            "cookie file (curl/wget)."
        ),
    )
    parser.add_argument(
        "--save-cookies",
        default=None,
        help=(
            "Yandex.Market only: persist the browser session "
            "cookies to this file (default: yandex_cookies.json). "
            "After a SmartCaptcha is solved — automatically or "
            "manually — the cookies are saved and auto-loaded on "
            "the next runs, so the challenge appears at most once "
            "per cookie lifetime. --cookies takes priority when "
            "both are given."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "public_page only: parallel widget-flow workers — "
            "browser tabs of one session sharding the review pages "
            "(default: 1 = sequential). NOTE (measured 2026-09-15): "
            "tabs of one browser session serialize on Ozon's side / "
            "the single proxy tunnel, so wall time stays roughly "
            "the same; kept as the foundation for multi-session "
            "sharding. For a real speedup today run several "
            "processes with different --proxy ports."
        ),
    )
    parser.add_argument(
        "--parallel-sessions",
        type=int,
        default=1,
        help=(
            "Run N collection PROCESSES with disjoint --start-page/"
            "--max-pages chunks (one proxy from --proxy-list per "
            "process), then merge with review_id dedup. Requires "
            "--max-pages. NOTE (measured): naked ?page=N caps at "
            "~5 productive pages per session even with cookies, so "
            "for ONE product this only parallelizes the first "
            "~5 pages — the deep widget flow stays sequential. Best "
            "for running many products in parallel."
        ),
    )
    parser.add_argument(
        "--no-block-assets",
        action="store_true",
        help=(
            "Do not abort image/font/media requests on scraper "
            "pages (blocking them is the default: review photos "
            "dominate the ~880KB page and we only need their src "
            "urls). Applies to public_page AND playwright "
            "transports."
        ),
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help=(
            "Save a full-page screenshot into the debug dir on "
            "every debug dump (public_page/playwright transports). "
            "OFF by default: screenshots of a logged-in session "
            "are a PII hazard and slow every page down; HTML/JSON "
            "dumps are written regardless."
        ),
    )
    parser.add_argument(
        "--no-widget-scroll",
        action="store_true",
        help=(
            "Disable the scroll-mix phase of the widget flow (the "
            "default scrolls each review page like a reader, waits "
            "up to 1.5s for lazily appended cards, then moves to "
            "the next page)."
        ),
    )
    parser.add_argument(
        "--free-proxy-country",
        default=None,
        help=(
            "--free-proxy only: filter proxies by country. "
            "Comma-separated ISO country codes, e.g. 'RU' or "
            "'RU,UA,KZ'. DEFAULT: 'RU' (Russian proxies first, "
            "then CIS fallback: Belarus, Ukraine, Kazakhstan)."
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
        "--no-extra-streams",
        action="store_true",
        help=(
            "Disable the additional review-stream sorts. Default "
            "(enabled): after the default stream ends (~34 pages / "
            "~1000 reviews window, measured), the adapter walks "
            "extra sorts (score_asc, score_desc) which expose "
            "different windows — notably the low-star reviews "
            "nearly absent from the default one — and merges them "
            "by review_id. Roughly doubles the collection at the "
            "cost of extra pages."
        ),
    )
    parser.add_argument(
        "--parallel-streams",
        action="store_true",
        help=(
            "Ozon pagination only: run all review streams "
            "(default, score_asc, score_desc) CONCURRENTLY — each "
            "in its own browser session. Wall time ~= the slowest "
            "stream instead of the sum (~3x faster for the "
            "default 3-stream configuration). Slightly higher "
            "request rate from Ozon's perspective; debug dumps go "
            "into per-stream page_N_<sort> directories."
        ),
    )
    parser.add_argument(
        "--filter-streams",
        action="store_true",
        help=(
            "Ozon pagination only: add the withPhotos / withMedia "
            "filter streams. Each active filter is its own list "
            "ordering and therefore its own ~7k-review window — "
            "the only known lever for reviews that sit beyond all "
            "three sort windows. If the param is not honored the "
            "stream just re-walks the default window and "
            "--dup-streak-stop bounds the waste."
        ),
    )
    parser.add_argument(
        "--dup-streak-stop",
        type=int,
        default=300,
        help=(
            "Stop a review stream after this many CONSECUTIVE "
            "reviews already collected by other streams (default: "
            "300, i.e. ~10 pages). Protects against streams re-"
            "serving known ground. 0 disables the early stop."
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
    parser.add_argument(
        "--include-rating-only",
        action="store_true",
        help=(
            "Ozon only: append SYNTHETIC rows for rating-only "
            "«оценки» (stars without any text) so the file covers "
            "the full histogram count. Ozon never exposes these "
            "individually — rows are generated from the "
            "webReviewProductScore histogram with deterministic "
            "ids '<product_id>-ro-<stars>-<n>' and raw.synthetic="
            "true. A .summary.json file is written regardless of "
            "this flag (when the histogram is available)."
        ),
    )
    parsed = parser.parse_args(argv)

    if not parsed.url and not parsed.products_file:
        parser.error(
            "--url is required unless --products-file is given"
        )
    if parsed.url and parsed.products_file:
        parser.error("--url and --products-file are mutually exclusive")
    if (
        parsed.products_file
        and parsed.marketplace != "ozon"
    ):
        parser.error(
            "--products-file supports only --marketplace ozon"
        )
    return parsed


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


def _scan_output_ratings(
    output: Path,
    product_id: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """Scan the output JSONL: total rows per star and existing
    synthetic rating-only rows per star (ids prefixed
    ``<product_id>-ro-<star>-``).
    """
    per_star: dict[str, int] = {}
    synth: dict[str, int] = {}
    prefix = f"{product_id}-ro-"
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
                rating = record.get("rating")
                if rating is None or isinstance(rating, bool):
                    continue
                try:
                    star = str(int(rating))
                except (TypeError, ValueError):
                    continue
                per_star[star] = per_star.get(star, 0) + 1
                rid = str(record.get("review_id") or "")
                if rid.startswith(f"{prefix}{star}-"):
                    synth[star] = synth.get(star, 0) + 1
    except OSError:
        pass
    return per_star, synth


def _finalize_rating_summary(
    *,
    output: Path,
    summary: dict[str, Any],
    include_rating_only: bool,
    marketplace: str,
) -> int:
    """Write ``<output>.summary.json``; with ``include_rating_only``
    also append synthetic rows for the rating-only remainder.

    Returns the number of synthetic rows appended this run.
    """
    histogram = summary.get("histogram") or {}
    product_id = str(summary.get("product_id") or "")
    if not histogram or not product_id:
        return 0

    per_star, synth = _scan_output_ratings(output, product_id)

    remainder = {
        star: max(0, int(count) - per_star.get(star, 0))
        for star, count in histogram.items()
    }

    added = 0
    if include_rating_only and any(remainder.values()):
        with output.open("a", encoding="utf-8") as file:
            for star in sorted(remainder, reverse=True):
                need = remainder[star]
                if need <= 0:
                    continue
                start = synth.get(star, 0)
                for n in range(start + 1, start + need + 1):
                    record = {
                        "review_id": (
                            f"{product_id}-ro-{star}-{n:05d}"
                        ),
                        "product_id": product_id,
                        "marketplace": marketplace,
                        "rating": int(star),
                        "text": None,
                        "author": None,
                        "created_at": None,
                        "pros": None,
                        "cons": None,
                        "seller_answer": None,
                        "raw": {
                            "synthetic": True,
                            "source": (
                                "webReviewProductScore histogram"
                            ),
                            "note": (
                                "Оценка без отзыва: Ozon не отдаёт "
                                "такие записи по отдельности"
                            ),
                        },
                    }
                    file.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
                    added += 1

    summary_record = {
        "product_id": product_id,
        "product_url": summary.get("product_url"),
        "average_score": summary.get("average_score"),
        "site_ratings_total": summary.get("reviews_count"),
        "site_histogram": histogram,
        "rows_per_star_in_file": per_star,
        "rating_only_per_star": remainder,
        "synthetic_rows_appended_this_run": added,
        "synthetic_rows_total_in_file": sum(synth.values()) + added,
    }
    summary_path = Path(str(output) + ".summary.json")
    summary_path.write_text(
        json.dumps(
            summary_record,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    histogram_preview = " ".join(
        f"{star}*={count}"
        for star, count in sorted(
            histogram.items(),
            reverse=True,
        )
    )
    print(
        f"Ozon: гистограмма оценок: {histogram_preview}; "
        f"оценок без текста (нельзя собрать индивидуально): "
        f"{sum(remainder.values())}"
        + (
            f"; добавлено синтетических строк: {added}"
            if added
            else ""
        )
        + f"; сводка: {summary_path.name}"
    )
    return added


async def _collect_ozon(args: argparse.Namespace) -> int:
    # Lazy import: heavy transport modules are imported only when
    # the user selects them.
    from infrastructure.marketplaces.ozon import OzonAdapter

    if not args.debug_dir:
        args.debug_dir = "debug_ozon"

    transport = await _build_ozon_transport(args)
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
                extra_streams=not args.no_extra_streams,
                parallel_streams=getattr(
                    args, "parallel_streams", False,
                ),
                filter_streams=getattr(
                    args, "filter_streams", False,
                ),
                dup_streak_stop=getattr(
                    args, "dup_streak_stop", 300,
                ),
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

    # Ozon: rating histogram summary. Rating-only «оценки» (stars
    # without text) are not exposed individually by Ozon — only as
    # histogram counts. Write a summary file and, with
    # --include-rating-only, append synthetic rows for the remainder.
    summary = getattr(adapter, "last_rating_summary", None)
    if isinstance(summary, dict) and summary.get("histogram"):
        added = _finalize_rating_summary(
            output=output,
            summary=summary,
            include_rating_only=getattr(
                args, "include_rating_only", False,
            ),
            marketplace=adapter.name,
        )
        count += added

    return count


async def _build_ozon_transport(args: argparse.Namespace):
    """Construct the Ozon transport based on --transport.

    Returns an object that implements the OzonBrowserTransport
    Protocol (iter_ozon_reviews_json, iter_ozon_reviews_by_scroll,
    iter_all_ozon_reviews, get_ozon_reviews_json).
    """
    # Build proxy pool / single proxy from CLI args.
    from infrastructure.transports.proxy_pool import proxy_to_url

    proxy_pool = await _build_proxy_pool(args)
    single_proxy = _build_single_proxy(args) if proxy_pool is None else None

    # playwright/hybrid drive ONE browser session and accept a
    # single proxy. Without this, --proxy-list would be silently
    # ignored for them and ALL traffic would go direct from this
    # machine (privacy + rotation loss). Take the next proxy from
    # the pool for the whole run.
    if (
        proxy_pool is not None
        and args.transport in ("playwright", "hybrid")
    ):
        pool_next = getattr(proxy_pool, "next_async", None)
        single_proxy = (
            await pool_next()
            if pool_next is not None
            else proxy_pool.next()
        )
        if single_proxy is None:
            print(
                "[warning] все proxy пула заблокированы — "
                "запуск напрямую с этого IP"
            )
        else:
            print(
                f"[info] {args.transport}-транспорт: один proxy на "
                f"весь запуск — {single_proxy.get('server', '?')} "
                "(построчная ротация только у public_page)"
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
            cookies=cookies,
            workers=args.workers,
            widget_scroll=not args.no_widget_scroll,
            block_assets=not args.no_block_assets,
            screenshots=args.screenshots,
        )

    if args.transport == "curl_cffi":
        from infrastructure.transports.curl_cffi import (
            CurlCffiTransport,
        )
        # curl_cffi takes a proxy URL string, not a dict.
        proxy_url = (
            proxy_to_url(single_proxy)
            if single_proxy is not None
            else None
        )
        return CurlCffiTransport(
            timeout=args.timeout_ms / 1000.0,
            debug_dir=args.debug_dir,
            impersonate=args.impersonate,
            proxy=proxy_url,
        )

    if args.transport == "hybrid":
        from infrastructure.transports.hybrid import HybridTransport
        # Hybrid takes a playwright proxy dict + curl_cffi proxy URL.
        curl_proxy_url = (
            proxy_to_url(single_proxy)
            if single_proxy is not None
            else None
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
                "proxy": single_proxy,
                "humanize": not args.no_humanize,
                "fetch_strategy": args.fetch_strategy,
                "stealth": not args.no_stealth,
                "cookies": cookies,
                "block_assets": not args.no_block_assets,
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
        cookies=cookies,
        screenshots=args.screenshots,
        block_assets=not args.no_block_assets,
    )


async def _build_proxy_pool(args: argparse.Namespace):
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


async def _collect_yandex(args: argparse.Namespace) -> int:
    """Collect Yandex.Market reviews into the output JSONL."""
    from infrastructure.marketplaces.yandex import (
        YandexMarketAdapter,
    )
    from infrastructure.transports.yandex_browser import (
        YandexBrowserTransport,
    )

    # One proxy for the whole run (a browser session must keep a
    # stable egress IP).
    proxy = None
    if args.proxy:
        proxy = _build_single_proxy(args)
    elif args.proxy_list:
        pool = await _build_proxy_pool(args)
        if pool is not None:
            pool_next = getattr(pool, "next_async", None)
            proxy = (
                await pool_next()
                if pool_next is not None
                else pool.next()
            )
            if proxy is not None:
                print(
                    f"[info] yandex-транспорт: один proxy на весь "
                    f"запуск — {proxy.get('server', '?')}"
                )

    cookies = None
    if args.cookies:
        from infrastructure.transports.cookie_loader import (
            load_cookies_file,
        )
        cookies = load_cookies_file(args.cookies)
        print(
            f"Я.Маркет: загружено cookies из {args.cookies}: "
            f"{len(cookies)} шт."
        )

    debug_dir = args.debug_dir or "debug_yandex"

    transport = YandexBrowserTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=debug_dir,
        proxy=proxy,
        cookies=cookies,
        humanize=not args.no_humanize,
        cookies_path=(
            args.save_cookies or "yandex_cookies.json"
        ),
    )
    adapter = YandexMarketAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.resume:
        seen_ids = _load_existing_reviews(output)
        if seen_ids:
            print(
                f"Resume: {len(seen_ids)} отзывов уже в "
                f"{output.name}, будут пропущены."
            )
        file_mode = "a"
    else:
        seen_ids = set()
        file_mode = "w"

    count = 0

    with output.open(file_mode, encoding="utf-8") as file:
        async for review in adapter.iter_reviews(args.url):
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

            if (
                args.max_reviews is not None
                and count >= args.max_reviews
            ):
                print(
                    f"Достигнут лимит --max-reviews: "
                    f"{args.max_reviews}"
                )
                break

    total = adapter.last_total_count
    if total is not None:
        print(
            f"Я.Маркет: по данным сайта всего отзывов: {total}; "
            f"собрано: {count}"
        )

    return count


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
        return await _collect_yandex(args)
    raise SystemExit(f"Unknown marketplace: {args.marketplace}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if getattr(args, "products_file", None):
            from marketplace_maps_parser.parallel_sessions import (
                run_products_parallel,
            )
            count = asyncio.run(run_products_parallel(args))
            print(f"Собрано отзывов: {count}")
            return 0

        if getattr(args, "parallel_sessions", 1) > 1:
            from marketplace_maps_parser.parallel_sessions import (
                run_parallel_sessions,
            )
            count = asyncio.run(run_parallel_sessions(args))
            print(f"Собрано отзывов: {count}")
            return 0

        count = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130

    print(f"Сбор завершён. Всего отзывов: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
