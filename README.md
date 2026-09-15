# marketplace-maps-parser

Asynchronous scraper for product reviews from Russian e-commerce marketplaces (Ozon, Wildberries, Yandex Market — _planned_). Streams normalized review records to JSONL.

## Features

- **Async streaming** — review records flow through `async for` iterators, so memory stays flat even on products with thousands of reviews.
- **Anti-bot evasion for Ozon** — uses [`invisible-playwright`](https://pypi.org/project/invisible-playwright/) to drive a stealth Chromium, then issues `fetch()` inside the page context so requests carry Ozon's Cloudflare-issued cookies.
- **Two Ozon strategies** — pagination via internal `entrypoint-api.bx/page/json/v2` endpoint, or DOM-scrape via `[data-review-uuid]` cards.
- **Schema-tolerant parsing** — `walk_json()` recursively walks the entire Ozon payload and identifies review nodes by a fuzzy marker set, so minor API changes don't break extraction.
- **Per-page deduplication** — composite key fallback (`product_id|page|position|author|date|rating|text`) when `review_id` is missing.
- **Debug-first** — every page run dumps HTML, screenshot, response log, and captured JSON to `debug_ozon/` for postmortem analysis.

## Requirements

- Python **3.14+**
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Chromium binaries installed via `playwright install chromium`

## Installation

```bash
# clone
git clone https://github.com/robert-werner/marketplace-maps-parser.git
cd marketplace-maps-parser

# install (uv)
uv sync
uv run playwright install chromium

# or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

## Configuration

Copy `.env.example` to `.env` and fill in marketplace credentials:

```bash
cp .env.example .env
```

| Variable        | Required for          | Description                       |
|-----------------|-----------------------|-----------------------------------|
| `WB_API_TOKEN`  | Wildberries           | Not currently used; reserved      |
| `OZON_CLIENT_ID`| Ozon (official API)   | Stub transport only               |
| `OZON_API_KEY`  | Ozon (official API)   | Stub transport only               |
| `PROXY`         | Optional              | HTTP/S proxy URL for scraping     |

## Usage

### CLI

```bash
# Ozon reviews via auto strategy (DEFAULT — pagination + scroll fallback)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890" \
  --output ozon_reviews.jsonl

# Ozon reviews via DOM scroll only
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --strategy scroll \
  --output ozon_reviews_scroll.jsonl

# Ozon reviews via internal pagination API only
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --strategy pagination \
  --max-pages 10 \
  --output ozon_reviews_paged.jsonl

# Cap total reviews across both strategies
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --max-reviews 500

# Resume an interrupted run (skip already-collected reviews)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --output ozon_reviews.jsonl \
  --resume

# Tune per-page retry on transient Cloudflare blocks
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --retry-attempts 5

# Disable stealth init script (for debugging)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --no-stealth

# Use curl_cffi (TLS-fingerprint impersonation, faster than Playwright)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --transport curl_cffi \
  --impersonate chrome120

# Use curl_cffi with a different browser fingerprint
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --transport curl_cffi \
  --impersonate firefox120

# Use hybrid (curl_cffi first, Playwright fallback on Cloudflare challenge)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --transport hybrid

# Use public_page (default — scrape the public review page DOM, least Cloudflare friction)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --transport public_page

# Use public_page with per-page fingerprint randomization (maximal stealth)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --transport public_page \
  --randomize-fingerprint

# Use a single residential proxy (avoids Cloudflare "Выключите VPN" blocks)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --proxy "http://user:pass@residential.proxy.com:8080"

# Use a proxy list with per-page rotation (best anti-blocking)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --proxy-list proxies.txt \
  --transport public_page \
  --randomize-fingerprint

# Use legacy in-page fetch (faster but more Cloudflare 403s)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --fetch-strategy fetch

# Default: navigate directly to API URL (Cloudflare-friendly)
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --fetch-strategy navigation

# Wildberries reviews via public API
uv run python -m marketplace_maps_parser \
  --marketplace wildberries \
  --url "https://www.wildberries.ru/catalog/12345678/detail.aspx" \
  --output wb_reviews.jsonl

# Limit pages
uv run python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --max-pages 5
```

### As a library

```python
import asyncio
from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.transports.browser_json import BrowserJsonTransport

async def main():
    transport = BrowserJsonTransport(
        timeout_ms=90_000,
        settle_ms=2_000,
        debug_dir="debug_ozon",
        humanize=True,
    )
    adapter = OzonAdapter(browser_transport=transport)

    # auto: pagination first, scroll as fallback / supplement
    # All reviews are deduplicated by review_id across both strategies.
    async for review in adapter.iter_all_reviews(
        product_url="https://www.ozon.ru/product/...",
        strategy="auto",
        max_reviews=None,
    ):
        print(review.review_id, review.rating, (review.text or "")[:80])

asyncio.run(main())
```

### Strategy reference

| `--strategy`  | Behavior |
|----------------|----------|
| `auto` (default) | Run pagination first; then run scroll to catch any reviews the API missed (or failed to return). Cross-strategy dedup by `review_id` / `uuid`. Most complete. |
| `pagination`     | Only the internal `entrypoint-api.bx/page/json/v2` endpoint. Fast but Cloudflare-protected. |
| `scroll`         | Only DOM scroll. Slower but resilient against API blocks. |

### Resume and retry

`--resume` reads existing `review_id` values from `--output` before
starting, skips reviews that are already in the file, and appends new
ones (instead of overwriting). Use it when:

- A previous run was interrupted (Ctrl-C, network outage) and you want
  to continue without re-scraping everything.
- You ran with `--strategy pagination` first and want to fill gaps with
  a second `--strategy auto --resume` run.

`--retry-attempts N` controls per-page retries when the internal Ozon
API returns a transient failure (HTTP non-200, non-JSON, or unparseable
body). Retries use exponential backoff with jitter:

```
delay = min(1.5 * 2^(n-1), 15s) * (1 ± 0.3)
```

Only `RuntimeError` from `_fetch_json_inside_page` is retried. Other
exceptions (network timeouts, browser navigation errors) propagate
immediately — they typically indicate the browser session itself is
unhealthy and a same-page retry would not help.

### Fetch strategy

`--fetch-strategy` controls how the Ozon API endpoint is hit:

| `--fetch-strategy` | What it does | When to use |
|---|---|---|
| `navigation` (default) | `page.goto(api_url)` — opens the API URL directly in the browser tab. Cloudflare sees a real browser navigation and is much less likely to return 403. | When Cloudflare blocks the legacy fetch strategy (typical production scenario). |
| `fetch` (legacy) | `page.evaluate(fetch(api_url))` — calls `fetch()` from the page's JS context. Faster (no full page navigation) but Cloudflare distinguishes this from a real browser navigation and returns 403 more aggressively. | When the navigation strategy is too slow, or for local testing without Cloudflare protection. |

### Stealth mode

Stealth mode (`playwright`/`hybrid` transports) applies an init script to every fresh browser page that patches the most common signals Cloudflare uses to detect automated browsers:

- `navigator.webdriver` → `undefined`
- `window.chrome.runtime` → fake object (real Chrome exposes it)
- `Notification.permission` → `'default'` (headless reports `'denied'`)
- `navigator.plugins` → fake PDF viewer entries
- `navigator.mimeTypes` → fake PDF mime types
- `navigator.languages` → `['ru', 'ru-RU', 'en-US', 'en']`
- `window.outerWidth` / `outerHeight` → non-zero values (headless reports `0`)
- `navigator.permissions.query` for notifications → `'default'`

**IMPORTANT — the `public_page` transport does NOT apply this script** (and `--no-stealth` has no effect on it). Measured 2026-09-15: with the script Ozon serves its «Похоже, нет соединения» error page (0 review cards); without it the same proxy/fingerprint gets HTTP 200 and 30 cards — on both Firefox (invisible-playwright) and Chromium engines. The fake `navigator.plugins` lacks the `PluginArray` methods Ozon's page JS expects, and a fake `window.chrome` contradicts a Firefox engine. invisible-playwright already provides the real stealth (patched engine, randomized fingerprint, humanized input), so on `public_page` the extra JS layer only breaks things.

### Antibot hardening (public_page)

Layered defenses against Ozon's antibot (all enabled by default):

1. **Warmup navigation** — before hitting `/reviews?page=N`, the transport lands on the product page, waits 0.8–2.2 s (randomized), then navigates to the reviews URL with the product page as the HTTP referer. Cold referer-less hits on `/reviews` are a strong bot signal.
2. **Challenge detection** — a fetched page is classified as an antibot/challenge page by title (`Antibot Challenge Page`, «Похоже, нет соединения», `Just a moment…`, Cloudflare blocks) and unambiguous Cloudflare HTML markers (`__cf_chl`, `challenge-platform`, «Выключите VPN»). A real reviews page that merely contains the string `antibot` in its HTML is NOT flagged (it does — measured).
3. **Rotate and retry** — on a challenge, the proxy is marked blocked in the pool, the transport cools down (5 s → 30 s backoff) and re-fetches the SAME page through the next proxy with a fresh fingerprint.
4. **Session-failure rotation** — any browser-session-level failure (proxy refused CONNECT, egress-IP discovery failed, `ProxyEgressDrifted` on non-sticky rotating gateways) is converted to "rotate the proxy and retry the page" instead of crashing the run.

The script is applied via `page.add_init_script` so it runs before any page JS executes (only on the `playwright`/`hybrid` transports that hit the internal API — see the note above for why `public_page` skips it).

If stealth causes issues with a particular Ozon layout, disable it temporarily with `--no-stealth` for debugging.

### Transport: public_page vs playwright vs curl_cffi vs hybrid

`--transport` selects between four Ozon transport implementations:

| `--transport` | What it does | Strengths | Limitations |
|---|---|---|---|
| `public_page` (default) | Scrapes the **public review page** DOM (`/product/<id>/reviews?page=N`) — no internal API | Least Cloudflare friction (page is on the public CDN, not the API); supports both `--strategy pagination` and `--strategy scroll`; full DOM access; uses the same stealth init script as `playwright` | Slower than `curl_cffi` (full browser, page rendering per request); relies on the page DOM staying stable |
| `playwright` | Uses invisible-playwright to drive a real browser hitting the **internal API** endpoint (`/api/entrypoint-api.bx/page/json/v2`) | Solves Cloudflare JS challenges automatically (browser runs the embedded JS); supports `--strategy scroll`; full DOM access | Heavy (Chromium process); slow (full page rendering per request); internal API is heavily protected by Cloudflare (many 403 challenges) |
| `curl_cffi` | Uses curl_cffi (libcurl with curl-impersonate) to hit the internal API endpoint | 10-50x faster than Playwright (no browser startup); true browser TLS fingerprint at the byte level (JA3/JA4 match Chrome/Firefox); tiny memory footprint | Cannot solve Cloudflare JS challenges (no JS engine); `--strategy scroll` not supported (no DOM); only `--strategy pagination` |
| `hybrid` | Tries curl_cffi first, falls back to Playwright on persistent CloudflareChallengeError | Fast when Cloudflare is permissive (curl_cffi handles most pages); robust when Cloudflare challenges (Playwright solves the JS challenge on the failing page) | Some pages incur the Playwright startup cost on first challenge; the fallback is per-page, so subsequent pages still try curl_cffi first |

Use **`public_page`** (default) when:
- You want the **least Cloudflare friction** — the public review page is served by Ozon's CDN, not the API endpoint, and is much less aggressively protected
- You're OK with a full browser (slower than curl_cffi but more reliable)
- You want both pagination and scroll strategies available

Use **`curl_cffi`** when:
- Cloudflare is blocking based on TLS fingerprint (JA3/JA4 hash)
- You need to scrape many products fast
- You're OK with pagination-only (no scroll fallback)
- Cloudflare's bot protection is permissive enough that warmup with the product page is sufficient

Use **`playwright`** when:
- Cloudflare returns the HTML "enable JavaScript" challenge page that requires a real browser to solve
- You need `--strategy scroll` for products where pagination misses reviews
- The internal API is the only way to get the data you need

Use **`hybrid`** when:
- You want the speed of curl_cffi but need a safety net for Cloudflare challenges
- Cloudflare challenges are intermittent (most pages work with curl_cffi, but some need Playwright)
- You're not sure which transport to use — hybrid tries the fast path first

`--impersonate` (curl_cffi and hybrid) selects which browser TLS fingerprint to use. Default: `chrome120`. Other useful values: `chrome119`, `firefox120`, `safari17_0`. See the [curl_cffi docs](https://curl-cffi.readthedocs.io/) for the full list.

#### Cloudflare warmup (curl_cffi and hybrid)

curl_cffi and hybrid transports perform a "warmup" request to the product page before the first API request. This obtains the Cloudflare bot-management cookies (`__cf_bm`, `cf_clearance`) that gate access to the API endpoint. Without warmup, the API endpoint returns HTTP 403 with a challenge body on every cold-session request.

The warmup happens only once per transport lifetime (tracked via the `_warmed_up` flag), not on every iteration of `iter_ozon_reviews_json`.

If the warmup page itself returns a Cloudflare challenge (curl_cffi cannot solve JS challenges), the transport logs a warning and continues — the API request will likely also fail, in which case:
- For `--transport curl_cffi`: the request fails with `CloudflareChallengeError` and retries with backoff until exhausted.
- For `--transport hybrid`: after curl_cffi retries are exhausted, the hybrid transport switches to Playwright for that page.

#### Why public_page is the default

The internal API endpoint (`/api/entrypoint-api.bx/page/json/v2`) is heavily protected by Cloudflare — even with TLS-fingerprint impersonation, stealth init scripts, and JS-challenge auto-resolution, a significant fraction of requests get 403 challenges. The public review page (`/product/<id>/reviews?page=N`), by contrast, is served by Ozon's CDN to all visitors (including non-logged-in browsers) and is rarely challenged. The `public_page` transport scrapes this page's DOM directly, getting the same review data with much less Cloudflare friction.

#### Per-page fingerprint randomization (`--randomize-fingerprint`)

When `--randomize-fingerprint` is passed (public_page transport only), a fresh `InvisiblePlaywright` browser instance is created for **each page** instead of reusing one for the whole pagination run. Each new browser gets a new random fingerprint via `seed=None` (→ `secrets.randbits(31)`), which randomizes:

- GPU vendor and renderer (NVIDIA / Intel / AMD)
- Screen resolution (1920×1080, 2560×1440, etc.)
- Hardware concurrency (CPU cores), storage quota
- Audio sample rate, codec support
- WebGL MSAA samples, extensions
- Font manifest, ClearType settings
- Dark theme on/off

This makes every page look like a different browser to Cloudflare — even if one fingerprint gets flagged, the next page uses a completely new one.

**Trade-off**: slower (~2-5s browser startup per page vs. one-time startup for the whole run). Use this only when Cloudflare is actively fingerprinting your sessions and the default (one browser per run) is getting blocked.

Without `--randomize-fingerprint` (default), one `InvisiblePlaywright` browser is reused for all pages in a single pagination run — faster, but all pages share the same fingerprint.

#### Proxy pool with rotation (`--proxy` / `--proxy-list`)

When Cloudflare returns the "Выключите VPN, перезагрузите роутер или подключитесь к другой сети" block page (with an incident ID like `fab_chlg_...`), it means Cloudflare has identified your IP as a VPN/proxy/datacenter and is blocking at the **network level** — no fingerprint or stealth trick can help.

The only reliable solution is to use **residential proxies** (IPs from real ISPs) and to **rotate** them so a single blocked IP doesn't kill the whole run.

**`--proxy URL`** — use a single proxy for all requests:
```bash
python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --proxy "http://user:pass@residential.proxy.com:8080"
```

**`--proxy-list FILE`** — rotate through a list of proxies (one per line, `#` comments allowed):
```
# proxies.txt
http://user1:pass1@residential1.proxy.com:8080
http://user2:pass2@residential2.proxy.com:8080
socks5://user3:pass3@residential3.proxy.com:1080
```
```bash
python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --proxy-list proxies.txt \
  --transport public_page \
  --randomize-fingerprint
```

When `--proxy-list` is active, the transport automatically switches to **per-page browser** mode (each page gets a fresh browser with the next proxy in the rotation). Blocked proxies are marked and skipped on the next rotation. When all proxies are blocked, the transport falls back to direct connection with a warning.

**Best results**: combine `--proxy-list` (residential IPs) + `--randomize-fingerprint` (new browser fingerprint per page) + `--transport public_page` (scrape the CDN page, not the API). This gives you a different IP + different browser fingerprint + least-protected URL on every page — Cloudflare has nothing consistent to block on.

#### Free proxy pool (`--free-proxy`)

When you don't have a residential proxy list handy, `--free-proxy` automatically fetches free public proxies via the [`free-proxy`](https://pypi.org/project/free-proxy/) PyPI package and rotates them per page:

```bash
# Auto-fetch free proxies and rotate (defaults to Russian proxies first!)
python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --free-proxy

# Explicitly specify Russian proxies + elite (high-anonymity)
python -m marketplace_maps_parser \
  --marketplace ozon \
  --url "https://www.ozon.ru/product/..." \
  --free-proxy \
  --free-proxy-country RU \
  --free-proxy-elite
```

**Russian proxy priority** (default behavior when `--free-proxy-country` is not specified):

| Refill round | Countries tried | Description |
|---|---|---|
| Round 0 | `RU` | Russian proxies first — Ozon is a Russian marketplace, RU IPs are least likely to be blocked |
| Round 1 | `BY`, `UA`, `KZ` | CIS fallback — Belarus, Ukraine, Kazakhstan (geographically close to Russia) |
| Round 2+ | All countries | Last resort — no country filter, widest pool |

When all proxies from one round are blocked, the pool automatically advances to the next round and fetches a fresh batch. `reset_blocked()` resets the round counter back to 0 (RU first).

When all fetched proxies are blocked, `FreeProxyPool` automatically fetches a new batch (auto-refill). Blocked proxies from previous batches are remembered and skipped.

**⚠️ WARNING**: Free public proxies are:
- Usually **datacenter IPs** (not residential) — Cloudflare may still block them
- **Unreliable** — high failure rate, proxies go offline frequently
- **Slow** — high latency, limited bandwidth
- **Few RU proxies** — free-proxy typically has only 1-5 RU proxies, so CIS fallback kicks in quickly

For production scraping, use `--proxy-list` with **residential Russian proxies** from providers like Bright Data, Smartproxy, or IPRoyal. Use `--free-proxy` for development, testing, or quick prototyping.

## Architecture

The project follows a clean architecture layering — domain logic has zero infrastructure imports.

```
src/
├── domain/                # Pure business layer (no I/O)
│   ├── entities.py        # ProductRef, Review, ReviewPage dataclasses
│   └── ports.py           # Transport ABCs
├── application/           # Use-case orchestration
│   └── review_service.py  # Thin dispatcher over marketplace registry
├── infrastructure/        # Adapters
│   ├── marketplaces/
│   │   ├── base.py        # MarketplaceAdapter ABC
│   │   ├── registry.py    # Factory registry
│   │   ├── ozon.py        # Ozon adapter (pagination + scroll)
│   │   ├── wildberries.py # WB adapter (public API)
│   │   └── yandex_market.py  # TODO: stub
│   ├── transports/
│   │   ├── http.py           # httpx-based JSON transport (WB)
│   │   ├── browser.py        # Legacy XHR-capture transport
│   │   ├── browser_json.py   # In-page fetch + pagination (current)
│   │   └── browser_dom.py    # DOM-based extraction (scroll mode)
│   └── repositories/
│       └── jsonl_repository.py  # Append-only JSONL writer
├── app/                   # DI container (stub)
└── shared/
    └── url_parsers.py     # URL → product_id extractors
```

### Data flow

```
URL → UrlParser → ProductRef → MarketplaceAdapter.iter_reviews()
                                  ↓
                            Transport (httpx / invisible-playwright)
                                  ↓
                            Raw JSON payload
                                  ↓
                            walk_json + map_review_node  → Review
                                  ↓
                            JsonlReviewRepository.append()
```

## Roadmap

- [x] Unified "collect ALL reviews" flow with cross-strategy dedup
- [x] Structured logging via `loguru` (`shared/logging.py`)
- [x] Async retry + jittered backoff helpers (`shared/retry.py`)
- [x] Resume from existing JSONL (`--resume`)
- [x] Per-page retry of transient Playwright errors inside transport
- [ ] Yandex Market adapter
- [ ] `asyncpg` repository for direct DB writes
- [ ] Proper `pydantic-settings` config loader
- [ ] CI workflow (ruff + mypy + pytest)

## License

Proprietary. All rights reserved.
