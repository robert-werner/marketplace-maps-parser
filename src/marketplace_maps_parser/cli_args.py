# src/marketplace_maps_parser/cli_args.py
"""Command-line argument parsing (extracted from ``__main__.py``).

One function: :func:`parse_args` — the ~40-flag argparse tree for every
marketplace/transport/proxy/pacing option the CLI supports.
"""
from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="marketplace-maps-parser",
        description=(
            "Async review scraper for Ozon, Wildberries, "
            "Yandex Market, Yandex Maps, 2GIS and Avito profiles."
        ),
    )
    parser.add_argument(
        "--marketplace",
        choices=(
            "ozon",
            "wildberries",
            "yandex",
            "yandex_maps",
            "2gis",
            "avito",
        ),
        default=None,
        help=(
            "Target marketplace. OPTIONAL when --url is given: "
            "detected from the URL (market.yandex.ru -> yandex, "
            "yandex.ru/maps -> yandex_maps, ozon.ru -> ozon, "
            "wildberries.ru -> wildberries, 2gis.ru -> 2gis, "
            "avito.ru/brands -> avito). "
            "Required with --products-file (ozon only)."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="json",
        help=(
            "Output format (default: json). 'json' — the unified "
            "document: {reviews: […10 shared fields…], diagnostics: "
            "{status: complete/partial/failed, error, …}}; "
            "errors (captcha, blocks, "
            "transport failures) land in diagnostics, never as "
            "review records. 'jsonl' — the legacy one-record-per-"
            "line stream."
        ),
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
        default=None,
        help="Output path (default: reviews.json or reviews.jsonl).",
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
        "--dup-pages-stop",
        type=int,
        default=3,
        help=(
            "Yandex.Market: stop the walk after this many "
            "consecutive pages that add zero NEW reviews (past "
            "the last page Yandex re-serves old ground instead "
            "of an empty page). 0 disables the stop. "
            "Default: 3."
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
        "--checkpoint-interval",
        type=int,
        default=100,
        help=(
            "Flush the append-only checkpoint after this many new reviews "
            "(default: 100). Lower values reduce loss on interruption "
            "but increase disk I/O."
        ),
    )
    parser.add_argument(
        "--checkpoint-seconds",
        type=float,
        default=5.0,
        help=(
            "Flush checkpoints every N seconds while collecting (default: 5)."
        ),
    )
    parser.add_argument(
        "--debug-dir",
        default=None,
        help=(
            "Directory for HTML/JSON debug dumps. Default: "
            "debug_ozon (ozon), debug_yandex (yandex), "
            "debug_yandex_maps (yandex_maps)."
        ),
    )
    parser.add_argument(
        "--debug-dumps",
        action="store_true",
        help=(
            "Ozon: save raw JSON/HTML on every page (off by default). "
            "Slower and may contain account data; keep dumps private."
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
        default=750,
        help=(
            "Readiness grace after browser navigation "
            "(default: 750 ms)."
        ),
    )
    parser.add_argument(
        "--no-humanize",
        action="store_true",
        help="Disable invisible-playwright humanize mode.",
    )
    parser.add_argument(
        "--page-delay-seconds",
        type=float,
        default=0.8,
        help=(
            "Jittered delay between pagination page fetches in "
            "seconds (default: 0.8)."
        ),
    )
    parser.add_argument(
        "--scroll-pause-seconds",
        type=float,
        default=0.5,
        help=(
            "Pause between scroll steps in seconds "
            "(default: 0.5)."
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
        "--no-browser-api",
        action="store_true",
        help=(
            "Disable optional API fast paths for WB, Yandex.Market, "
            "Avito, Maps and 2GIS; keep browser DOM/SSR for comparison."
        ),
    )
    parser.add_argument(
        "--fetch-strategy",
        choices=("auto", "navigation", "fetch"),
        default="auto",
        help=(
            "Ozon: 'auto' (default) uses in-page fetch after review "
            "readiness, falling back to API navigation per tab. "
            "'fetch' and 'navigation' force one browser-only path."
        ),
    )
    parser.add_argument(
        "--no-stealth",
        action="store_true",
        help=(
            "Deprecated compatibility flag. Invisible Playwright owns "
            "the fingerprint in its browser engine; this flag does not "
            "inject or remove a page-level JavaScript shim."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("playwright",),
        default="playwright",
        help=(
            "Ozon: Invisible Playwright browser transport."
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
        "--proxy-attempts",
        type=int,
        default=3,
        help=(
            "Ozon with --proxy-list: retry an errored collection with "
            "a new browser session and the next proxy (default: 3 "
            "distinct entries, bounded). Does not rotate mid-session."
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
            "Yandex flows only: persist the browser session "
            "cookies to this file (default: yandex_cookies.json "
            "for yandex, yandex_maps_cookies.json for "
            "yandex_maps). After a SmartCaptcha is solved — "
            "automatically or manually — the cookies are saved "
            "and auto-loaded on the next runs, so the challenge "
            "appears at most once per cookie lifetime. --cookies "
            "takes priority when both are given."
        ),
    )
    parser.add_argument(
        "--parallel-sessions",
        type=int,
        default=1,
        help=(
            "Ozon/Yandex: independent Invisible Playwright sessions over "
            "disjoint page ranges (default: 1). Ozon requires --max-pages; "
            "Yandex can probe the total first. Each session uses a proxy "
            "from --proxy-list when provided."
        ),
    )
    parser.add_argument(
        "--no-block-assets",
        action="store_true",
        help=(
            "Do not abort image/font/media requests on scraper "
            "pages (blocking them is the default: review photos "
            "dominate the ~880KB page and we only need their src "
            "urls). Applies to the Ozon Invisible Playwright transport."
        ),
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help=(
            "Save a full-page screenshot into the debug dir on "
            "every debug dump (Ozon browser transport). "
            "OFF by default: screenshots of a logged-in session "
            "are a PII hazard and slow every page down. "
            "Also enables --debug-dumps."
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
        default=True,
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
        "--serial-streams",
        dest="parallel_streams",
        action="store_false",
        help="Ozon: disable concurrent review-stream workers.",
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
            "serving known ground. 0 disables the early stop "
            "(Ozon pagination; yandex_maps direct API — 0 drains "
            "every window fully, slowest and most complete)."
        ),
    )
    parser.add_argument(
        "--maps-api-concurrency",
        type=int,
        default=5,
        help=(
            "yandex_maps direct API: how many review streams "
            "(ranking × aspect windows) walk CONCURRENTLY "
            "(default: 5). Each stream keeps its own request "
            "pacing, so the server sees several slow scrollers "
            "rather than one fast bot; 1 restores the strictly "
            "serial walk."
        ),
    )
    parser.add_argument(
        "--maps-api-pacing",
        type=float,
        default=0.35,
        help=(
            "yandex_maps direct API: pause between requests "
            "within ONE stream, seconds (default: 0.35). Lower = "
            "faster but less polite."
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
    if parsed.output is None:
        parsed.output = f"reviews.{parsed.format}"
    for name in (
        "checkpoint_interval", "checkpoint_seconds", "timeout_ms",
        "parallel_sessions", "products_sessions", "start_page",
        "retry_attempts", "maps_api_concurrency", "proxy_attempts",
    ):
        if getattr(parsed, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("max_reviews", "max_pages"):
        value = getattr(parsed, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in (
        "settle_ms", "page_delay_seconds", "scroll_pause_seconds",
        "maps_api_pacing", "dup_streak_stop", "dup_pages_stop",
    ):
        if getattr(parsed, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be non-negative")

    if not parsed.url and not parsed.products_file:
        parser.error(
            "--url is required unless --products-file is given"
        )
    if parsed.url and parsed.products_file:
        parser.error("--url and --products-file are mutually exclusive")
    if parsed.marketplace is None:
        if parsed.url:
            from shared.url_parsers import detect_marketplace

            detected = detect_marketplace(parsed.url)
            if detected is None:
                parser.error(
                    f"cannot detect the marketplace from the URL "
                    f"{parsed.url!r} — pass --marketplace explicitly"
                )
            parsed.marketplace = detected
            print(
                f"Маркетплейс определён по ссылке: {detected}"
            )
        else:
            # --products-file with no --url: nothing to detect from.
            parser.error(
                "--products-file requires an explicit "
                "--marketplace ozon"
            )
    if (
        parsed.products_file
        and parsed.marketplace != "ozon"
    ):
        parser.error(
            "--products-file supports only --marketplace ozon"
        )
    return parsed
