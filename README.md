# marketplace-maps-parser

Asynchronous scraper for product and organization reviews (Ozon, Wildberries, Yandex Market, Yandex Maps, 2GIS). Streams normalized review records to JSONL.

## Features

- **Async streaming** — review records flow through `async for` iterators, so memory stays flat even on products with thousands of reviews.
- **Anti-bot evasion for Ozon** — uses [`invisible-playwright`](https://pypi.org/project/invisible-playwright/) to drive a stealth Chromium, then issues `fetch()` inside the page context so requests carry Ozon's Cloudflare-issued cookies.
- **Two Ozon strategies** — pagination via internal `entrypoint-api.bx/page/json/v2` endpoint, or DOM-scrape via `[data-review-uuid]` cards.
- **Schema-tolerant parsing** — `walk_json()` recursively walks the entire Ozon payload and identifies review nodes by a fuzzy marker set, so minor API changes don't break extraction.
- **Per-page deduplication** — composite key fallback (`product_id|page|position|author|date|rating|text`) when `review_id` is missing.
- **Debug-first** — every page run dumps HTML, response log, and captured JSON to `debug_ozon/` for postmortem analysis (full-page screenshots are opt-in via `--screenshots`: they leak PII of a logged-in session and cost a share of the per-page wall time).
- **Multi-product supervisor** — `--products-file urls.txt` runs one child process per product (bounded by `--products-sessions`, one proxy per product) and merges everything into a single deduplicated JSONL.

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

### Logged-in session cookies (`--cookies`)

Anonymous visitors get a capped reviews widget: ~33 pages ≈ 990 unique reviews, then the «Дальше» button disappears (measured 2026-09-15). To collect the full list, export the cookies of a **logged-in** Ozon session and pass them via `--cookies` — they are injected into every browser page before the first navigation:

```bash
python -m marketplace_maps_parser --marketplace ozon --url "https://www.ozon.ru/product/..." \
  --output reviews.jsonl --transport public_page --proxy-list proxies.txt --cookies ozon_cookies.json
```

Accepted formats (auto-detected):

- **Playwright/DevTools JSON** — a list of `{name, value, domain, path, expires, secure, httpOnly, sameSite}` objects. Log in to ozon.ru in your browser, export the cookies with any cookie-editor extension (e.g. "EditThisCookie" / "Cookie-Editor" → Export → JSON), save as a `.json` file.
- **Netscape cookie file** — tab-separated `domain  flag  path  secure  expiry  name  value` lines with `#` comments, as produced by `curl`/`wget` and most CLI exporters.

Notes: `sameSite` values are normalized (invalid values become `Lax` — Playwright rejects anything else); entries without `name`/`value`/`domain` are skipped; a file with no valid cookies raises a clear error. Cookies expire — if the run suddenly caps at ~990 again, re-export a fresh file.

### The fast path: internal API + cookies + fetch strategy

```bash
python -m marketplace_maps_parser --marketplace ozon --url "https://www.ozon.ru/product/…"   --output reviews.jsonl --transport playwright --strategy pagination   --fetch-strategy fetch --proxy-list proxies.txt --cookies ozon_cookies.json --resume
```

Measured 2026-09-16 on a ~2k-review product with a logged-in session: **~3.5 s/page, zero Cloudflare incidents**, ratings for 100% of reviews, and it also delivers rating-only (textless) reviews the public widget never shows. Per-page comparison: widget ~8 s → API navigation ~4.9 s → API fetch ~3.5 s. The `fetch` strategy historically drew more Cloudflare 403s — with cookies it measured clean, but `navigation` (the default) remains the robust fallback if a product starts challenging.

### Collection speed

Per-page costs after the 2026-09 optimizations: all 30 cards of a page (text + rating + images) are extracted with a **single** `page.evaluate` round-trip (the old per-attribute reader took ~1.6 s per page — 208× slower on the read alone); the widget's content-replacement poll runs at 300 ms; the fixed 1.2 s settle was replaced by an adaptive wait for the rating SVGs to hydrate; and image/font/media requests are aborted via **extension-pattern** routes (`--no-block-assets` to disable). Do NOT route `"**/*"` to block assets — every routed request detours through Python and the detour costs more than the blocked bytes save (measured: ~10.5 s/page with a catch-all route vs ~8 s with patterns).

`--workers N` shards the review pages across N browser tabs of one session through a frontier queue. Fair warning, measured live: tabs of a single session serialize (one Firefox + one proxy tunnel), so wall time does **not** drop with N. It is kept as the foundation for multi-session sharding.

### Speed optimizations (2026-09-16)

- **Adaptive inter-page pacing** (`shared/pacing.py`) — the fixed `--page-delay-seconds` delay now shrinks geometrically after clean pages (down to ~0.4 s) and resets with a penalty whenever the transport detects an antibot/challenge event. Long clean runs get faster without raising the block rate when Ozon starts noticing.
- **Push-based card-replacement wait** — the widget flow's 300 ms Python-side poll loop is replaced by an in-page `MutationObserver` that resolves the moment the rendered card set changes (debounced 250 ms so a mid-render partial set isn't mistaken for the final one). Removes one round-trip per poll and up to 300 ms of latency per page. Falls back to the old polling when `page.evaluate` is unavailable.
- **Shorter repeat warmups** — the first warmup of a run pauses 0.8–2.2 s like a human reading the product card; every subsequent one (per-page-browser modes pay one per page) pauses only 0.4–1.0 s. Saves ~1 s per rotated page.
- **Non-blocking free-proxy pool** — `FreeProxyPool` refills (network fetches of new proxy batches) run in a worker thread via `asyncio.to_thread`, so the event loop is never frozen mid-run.
- **Screenshots off by default** — see the Features note above.

### Many products at once (`--products-file`)

The reliable wall-time multiplier (measured): one child process per product, each with its own browser and its own proxy. The supervisor bounds concurrency, forwards your flags, prints a per-product summary, and merges all parts into `--output` with review_id dedup (review ids are unique across products, so a combined file is safe):

```bash
python -m marketplace_maps_parser \
  --marketplace ozon \
  --products-file products.txt \
  --output all_reviews.jsonl \
  --transport public_page \
  --proxy-list proxies.txt \
  --cookies ozon_cookies.json \
  --products-sessions 3
```

`products.txt` is one product URL per line (`#` comments allowed). `--products-sessions` (default 3) caps how many children run at once. `--max-reviews` applies per product.

### Yandex.Market (`--marketplace yandex`)

Streams reviews from the public reviews page (`/card/<slug>/<id>/reviews?page=N`) via a stealth invisible-playwright session. Measured facts (2026-09-18):

- **Warmup + canonical slug** — the transport lands on the product card first (referer + cookies), captures the post-redirect URL and rebuilds the reviews URL from the CANONICAL slug: a wrong-slug card page redirects, but a wrong-slug `/reviews` just 404s («Нет такой страницы», non-retryable `YandexNotFoundError`).
- **Three page states, classified by SSR data** — healthy (JSON-LD reviews / aggregate / DOM cards present), captcha (redirect markers OR the INLINE SmartCaptcha shell: a ~16 KB page with title «Вы не робот?», `captcha_smart` assets and a POST form to `/checkcaptcha` — served at the reviews URL itself, no redirect), and markerless soft-block (a big page with widgets but no review SSR data). Captcha markers are only trusted on pages WITHOUT SSR data — a healthy megabyte page legitimately mentions SmartCaptcha in its own scripts.
- **Captcha escalation ladder** — (a) auto-wait ~6 s (the inline shell often resolves itself for a trusted fingerprint); (b) programmatic checkbox click (humanize=True drives a realistic cursor trajectory); (c) manual solving in the visible browser window (headed by default; the transport prints a prompt and polls up to 180 s, cookies are checkpointed right after); (d) cooldown 5 s → 30 s + `AdaptivePacer.record_block`. A captcha that survives it all restarts the browser on the NEXT proxy from `--proxy-list` with a fresh cookie jar (state — seen cards / page number — carries over).
- **LD-rating lookahead** — the JSON-LD block paginates INDEPENDENTLY of the DOM (page 2's LD carries the ratings for four page-1 cards), so each batch is held back until the next page is read and its ratings are merged in. Live check: 10/10 reviews with ratings, 0 with labels glued into the text.
- **Realistic profile** — `block_assets` is OFF by default for Yandex (a real browser loads images/fonts; SmartCaptcha weighs that), the same stealth init script as Ozon runs in every page, and the settle delay is jittered ±20 % per page.
- Rating-only «оценки» (textless) are not exposed individually — only the aggregate counter (`last_total_count`); expect `collected < total` on every product.

```bash
python -m marketplace_maps_parser \
    --marketplace yandex \
    --url "https://market.yandex.ru/card/<slug>/<id>" \
    --output yandex_reviews.jsonl \
    --proxy-list proxies.txt \
    --cookies yandex_cookies.json \
    --save-cookies yandex_cookies.json
```

`--cookies` injects a logged-in/visited session (Playwright DevTools JSON or Netscape format — the same loader as Ozon); `--save-cookies` (default `yandex_cookies.json`) checkpoints the session after every healthy page, so a captcha solved once — manually or automatically — sticks for the cookie lifetime.

### Yandex.Maps organizations (`--marketplace yandex_maps`)

Streams ALL reviews of one organization (e.g. a post office, a café, a shop) from its `/maps/org/<slug>/<id>/reviews/` page. Measured facts (2026-09-18/19, live orgs incl. a 4360-review hospital):

- **SSR state blob** — the reviews page ships the FIRST page (50 reviews) inside `<script class="state-view">` JSON: `reviewResults.reviews` (the same card shape as the internal API: `reviewId`, `author.name`, `text`, `rating`, `updatedTime` ISO, `reactions`, `photos`) + `reviewResults.params` (`count` / `totalPages`) + the org aggregate `ratingData` (`ratingValue` / `reviewCount` / `ratingCount`) + the `aspects` list with ids (`Персонал · 3184 отзыва`) — no UI clicking needed.
- **The `s` signature is reversed — direct API is the primary path.** Pages 2+ load via XHR to `/maps/api/business/fetchReviews?…&csrfToken=<hex:ts>&s=<signature>`. The signature was reversed from the maps-front-maps base chunk (webpack module 79409, 2026-09-19): `s = djb2_xor32(query_string_without_s)` with a case-insensitive key sort — verified 96/96 against live captured URLs (see `sign_maps_query`). The transport templates `reqId`/`sessionId`/`csrfToken` from the site's own first XHR (one pane scroll), then pages every (ranking, aspectId) combination DIRECTLY via in-page `fetch()`. Falls back to interception+UI-walk if the recipe stops validating.
- **The server window: first 600 reviews per stream (offset-based).** The cap is SERVER-side, not frontend (page 13 at pageSize=50 errors; at pageSize=25 the boundary moves to page 25 — always offset 600; `params.totalPages` advertises the true depth, e.g. 88 pages). Fresh `reqId`/`sessionId` do NOT unlock deeper pages; the window is bound to the (org, stream) pair.
- **The streams matrix is the coverage lever.** Rankings (measured: «По умолчанию»=`by_relevance_org`, «По новизне»=`by_time`, «Сначала положительные»=`by_rating_desc`, «Сначала отрицательные»=`by_rating_asc`; the enum has no other values) × aspects from the blob × the two tone rankings (`by_aspect_tone_desc/asc`) — any combination is valid via the direct API, including aspect × arbitrary ranking, which the UI never offers (chips fix the tone; a ranking click resets the aspect). Small aspects (≤600 reviews) fit entirely into one window. `--dup-streak-stop 0` drains every window fully (slowest, most complete); aspects ≤600 are pruned to a single ranking in that mode.
- **Measured coverage on the 4360-review hospital**: UI walk ~3050 (70 %) in ~40 min; direct API 3285 (75 %) in ~10 min; `--dup-streak-stop 0` full drain → **3359 (77 %)**. The remaining ~23 % sit beyond the 600-window of every (aspect × ranking) combination — mid-pack reviews of the huge aspects (Персонал: 3184) and untagged reviews beyond the four global windows. Logged-in cookies do NOT lift the window (verified with a Session_id jar: page 13+ still errors), nor do fresh `reqId`/`sessionId`, other pageSizes, or multi-`aspectId` values. Small orgs (≤600 reviews) collect 100 % (86/86 post office, 8/8 office).
- **No captcha observed on Maps** (10+ probe sessions incl. plain curl_cffi loads, all clean) — no escalation ladder is ported; a challenge page raises `YandexCaptchaError` with the page dumped to the debug dir.
- **Rating-only assessments** — like Ozon, Maps has textless «оценки»: `ratingData.ratingCount` (7852) vs `reviewCount` (4360) on the hospital. They are never listed individually; the CLI prints the remainder (`оценок без отзыва`).

```bash
python -m marketplace_maps_parser \
    --marketplace yandex_maps \
    --url "https://yandex.ru/maps/org/<slug>/<org_id>" \
    --output yandex_maps_reviews.jsonl
```

Proxy/cookies flags work the same as the Yandex.Market flow (`--proxy`, `--proxy-list`, `--cookies`, `--save-cookies` — default `yandex_maps_cookies.json`); debug dumps go to `debug_yandex_maps/`.

### 2GIS firms (`--marketplace 2gis`)

Collects ALL reviews of one firm (`2gis.ru/<city>/firm/<branch_id>`). Measured facts (2026-09-19, live firm 70000001063192616, 22/22 collected):

- **SSR-only data, no usable list API** — the reviews tab (`/tab/reviews`) ships the ENTIRE review list server-side inside `window.__REACT_QUERY_STATE__` (a dehydrated React-Query cache): the `fetchEntityReviews` query holds `pages[].items` (full cards: `id`, `date_created` ISO, `rating`, `text`, `user.name`, `official_answer` — the org's reply, mapped to `seller_answer` — plus `likes_count`, `emojis`, `trust_factors`) and the page meta (`total`, `rating`, `hasMore`). The widget's `public-api.reviews.2gis.com/3.0/branches/…` endpoint answers `total_count: 0` even from the site's own runtime (its ratings/summary/comments sub-endpoints work but do not list reviews); a cold curl_cffi session gets an ~11 KB shell without the state — hence the invisible-playwright transport: one page load, state read via `page.evaluate`, one batch.
- **Pagination** — unimplemented: `hasMore=true` on a huge firm (not observed yet) would print a warning with the remainder. The `--dup-streak-stop 0` full-drain machinery from yandex_maps does not apply here.

```bash
python -m marketplace_maps_parser \
    --marketplace 2gis \
    --url "https://2gis.ru/moscow/firm/70000001063192616" \
    --output 2gis_reviews.jsonl
```

### Endpoint probes (`scripts/probe_ozon_endpoints.py`)

Two hypotheses that decide the next big speedups, testable with your proxy/cookies:

```bash
python scripts/probe_ozon_endpoints.py \
  --url "https://www.ozon.ru/product/..." \
  --proxy "http://user:pass@residential.proxy:8080" \
  --cookies ozon_cookies.json
```

The probe has two fetch engines: `curl_cffi` (fast, but Cloudflare can 403-challenge it — observed on cold proxied sessions) and `playwright` (a real browser via the Cloudflare-friendly navigation strategy). `--engine auto` (the default) tries curl_cffi and falls back to playwright automatically when challenged; the SSR verdict is then computed from the raw in-page-fetch response body (before client JS mutates the DOM), so it stays a true SSR test.

1. **SSR probe** — does the public reviews page HTML contain `data-review-uuid` cards without JS? If yes, the browser can be dropped for pagination (10–50× per page).
2. **page_size probe** — does the internal API honor a bigger page size (`page_size`, `itemsPerPage`, … variants)? If any variant returns more nodes than the `?page=1` control, request count drops proportionally.
3. **cookie handoff probe** — can curl_cffi take over once a real browser has bootstrapped the Cloudflare cookies (cookies exported from the live browser context, tried against several impersonation targets)?

**Measured 2026-09-16** (residential proxy, logged-in cookies):

| Hypothesis | Verdict | Detail |
|---|---|---|
| SSR of the public reviews page | **YES** | Raw fetch response: HTTP 200, ~860 KB, `data-review-uuid` × 30 — identical to the post-render DOM (30 → 30). The page is fully server-rendered. |
| Internal API `page_size` | **NO** | All 16 parameter spellings at sizes 60/100 returned exactly 30 nodes — the 30-cards-per-page limit is server-side. |
| Browser cookies → curl_cffi handoff | **NO** | Live browser cookies (incl. Cloudflare ones) get HTTP 403 on curl_cffi with `chrome120`, `firefox135` and `firefox133` — the clearance is bound to the real browser's fingerprint. A browserless transport is **not possible**; the in-page `fetch()` from a live browser session is the single-product speed ceiling (~3.5 s/page, see the fast path below). |

Consequences: the per-request optimizations that remain are pacing (adaptive delay), push-based waits and warmup cost — all implemented; the only real wall-time multiplier is running many products in parallel (`--products-file`).

**How to actually parallelize today** — important measurements (2026-09-16, logged-in session):

1. **Several products** — `--products-file` (see above): one process per product with its own proxy, merged automatically. This is the reliable multiplier.
2. **`--parallel-sessions N`** splits one product's page range across N CLI child processes (one proxy each) and merges with review_id dedup. NOTE: the naked `?page=N` pagination caps at ~5 productive pages per session EVEN with cookies (measured: 30 requested pages → 150 unique reviews), so for a single product this only parallelizes the first ~5 pages — the widget flow (page_key URLs) remains the only deep path and it is inherently sequential per session.

```bash
# проверка: глубина 50, один процесс, одна страница
python -m marketplace_maps_parser --marketplace ozon --url "https://www.ozon.ru/product/…"   --output probe50.jsonl --transport public_page --strategy pagination   --start-page 50 --max-pages 1 --cookies ozon_cookies.json   --proxy "http://user:pass@pool.proxys.io:10100"
```

If `probe50.jsonl` has ~30 reviews — chunk the range (e.g. 194 pages ≈ 3 chunks) and run them in parallel, one proxy port per chunk. Then merge with dedup:

```bash
python -c "
import glob, json
seen = set()
with open('all.jsonl', 'w', encoding='utf-8') as out:
    for f in sorted(glob.glob('part*.jsonl')):
        for line in open(f, encoding='utf-8'):
            rid = json.loads(line)['review_id']
            if rid not in seen:
                seen.add(rid)
                out.write(line)
print(len(seen), 'unique reviews merged')
"
```

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
│   └── entities.py        # ProductRef, Review, ReviewPage dataclasses
├── infrastructure/        # Adapters
│   ├── marketplaces/
│   │   ├── base.py        # MarketplaceAdapter ABC
│   │   ├── registry.py    # Factory registry
│   │   ├── ozon.py        # Ozon adapter (pagination + scroll)
│   │   └── wildberries.py # WB adapter (public API)
│   ├── transports/
│   │   ├── base.py        # Shared Ozon transport helpers (URLs, payloads, challenge detection)
│   │   ├── http.py           # httpx-based JSON transport (WB)
│   │   ├── browser.py        # Legacy XHR-capture transport
│   │   ├── browser_json.py   # In-page fetch + pagination (current)
│   │   └── browser_dom.py    # DOM-based extraction (scroll mode)
│   └── repositories/
│       └── jsonl_repository.py  # Append-only JSONL writer
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
- [x] Adaptive inter-page pacing (`shared/pacing.py`)
- [x] Multi-product parallel supervisor (`--products-file`)
- [x] Endpoint hypothesis probes (SSR / page_size / cookie handoff — `scripts/probe_ozon_endpoints.py`, verdicts measured 2026-09-16)
- [ ] Yandex Market adapter
- [x] Yandex Maps org adapter (`--marketplace yandex_maps`, 86/86 on the live probe org, 2026-09-18)
- [ ] `asyncpg` repository for direct DB writes
- [ ] CI workflow (ruff + mypy + pytest)

## License

Proprietary. All rights reserved.
