# main.py
#
# DEPRECATED — use the unified CLI instead:
#
#     uv run python -m marketplace_maps_parser \
#         --marketplace ozon \
#         --url "https://www.ozon.ru/product/..." \
#         --output ozon_reviews.jsonl
#
# This file is kept only for backward reference and historical diff
# context. It will be removed in a future release.
from __future__ import annotations

import asyncio
import sys

from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.repositories.jsonl_repository import (
    JsonlReviewRepository,
)
from infrastructure.transports.browser_dom import (
    BrowserDomTransport,
)
from fp.fp import FreeProxy

async def collect_all_ozon_reviews(
    adapter: OzonAdapter,
    product_url: str,
) -> int:
    repository = JsonlReviewRepository("ozon_reviews.jsonl")

    count = 0

    async for review in adapter.iter_reviews(
        product_url,
        max_reviews=None,
    ):
        await repository.append(review)
        count += 1

        if count % 100 == 0:
            print(f"Собрано отзывов: {count}")

    return count


async def main() -> None:
    print(
        "[deprecation] main.py is deprecated; "
        "use `python -m marketplace_maps_parser` instead.",
        file=sys.stderr,
    )
    adapter = OzonAdapter(
        browser_transport=BrowserDomTransport(),
    )

    count = await collect_all_ozon_reviews(
        adapter=adapter,
        product_url=(
            "https://www.ozon.ru/product/"
            "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
        ),
    )

    print(f"Всего собрано отзывов: {count}")


if __name__ == "__main__":
    asyncio.run(main())
