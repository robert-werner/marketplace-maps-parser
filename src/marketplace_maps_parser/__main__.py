"""Unified CLI entrypoint for marketplace-maps-parser.

Consolidates the four legacy ``main*.py`` scripts into a single argparse-driven
program supporting the Ozon pagination strategy, Ozon DOM-scroll strategy, and
the Wildberries public-API adapter.

Heavy browser transports (``invisible-playwright``) are imported
lazily inside the collectors (see ``marketplace_maps_parser
.collectors``) so the CLI can still run ``--help`` without the
browser stack installed.

Argument parsing lives in ``marketplace_maps_parser.cli_args``;
the per-marketplace collectors live in
``marketplace_maps_parser.collectors``. Both are re-exported
here for backward compatibility (tests import ``parse_args`` and
``_load_existing_reviews`` from this module).

Usage (--marketplace is optional when --url is given — it is
detected from the URL's domain + path)::

    python -m marketplace_maps_parser \
        --url "https://www.ozon.ru/product/..." \
        --output ozon_reviews.jsonl

    python -m marketplace_maps_parser \
        --url "https://market.yandex.ru/card/<slug>/<id>" \
        --output yandex_reviews.jsonl \
        --cookies yandex_cookies.json
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from marketplace_maps_parser.cli_args import parse_args as parse_args
from marketplace_maps_parser.collectors import (
    _load_existing_reviews as _load_existing_reviews,
)


async def _run(args: argparse.Namespace) -> int:
    from marketplace_maps_parser.collectors import (
        _collect_2gis,
        _collect_ozon,
        _collect_wildberries,
        _collect_yandex,
        _collect_yandex_maps,
    )

    if args.marketplace == "ozon":
        return await _collect_ozon(args)
    if args.marketplace == "wildberries":
        return await _collect_wildberries(args)
    if args.marketplace == "yandex":
        return await _collect_yandex(args)
    if args.marketplace == "yandex_maps":
        return await _collect_yandex_maps(args)
    if args.marketplace == "2gis":
        return await _collect_2gis(args)
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

        # Ozon: page-range CHILD PROCESSES (public_page chunks).
        # Yandex handles --parallel-sessions in-process inside its
        # collector (independent browser launches per range).
        if (
            args.marketplace == "ozon"
            and getattr(args, "parallel_sessions", 1) > 1
        ):
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
