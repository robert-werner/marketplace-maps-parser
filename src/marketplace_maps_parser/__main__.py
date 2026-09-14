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


async def _collect_ozon(args: argparse.Namespace) -> int:
    # Lazy import: invisible-playwright is heavy and may not be installed
    # in environments that only use the Wildberries path.
    from infrastructure.marketplaces.ozon import OzonAdapter
    from infrastructure.transports.browser_json import (
        BrowserJsonTransport,
    )

    transport = BrowserJsonTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=args.debug_dir,
        humanize=not args.no_humanize,
    )
    adapter = OzonAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    seen_ids: set[str] = set()
    count = 0

    with output.open("w", encoding="utf-8") as file:
        async for review in adapter.iter_all_reviews(
            product_url=args.url,
            strategy=args.strategy,
            max_reviews=args.max_reviews,
            pagination_max_pages=args.max_pages,
            pagination_start_page=args.start_page,
            page_delay_seconds=args.page_delay_seconds,
            scroll_pause_seconds=args.scroll_pause_seconds,
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
