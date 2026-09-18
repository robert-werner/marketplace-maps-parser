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


