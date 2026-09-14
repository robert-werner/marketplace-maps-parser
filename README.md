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
- [ ] Yandex Market adapter
- [ ] `asyncpg` repository for direct DB writes
- [ ] Proper `pydantic-settings` config loader
- [ ] CI workflow (ruff + mypy + pytest)
- [ ] Resume-from-checkpoint (JSONL tail resume on rerun)
- [ ] Per-page retry of transient Playwright errors inside transport

## License

Proprietary. All rights reserved.
