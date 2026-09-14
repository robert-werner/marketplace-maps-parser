# main_3.py
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
import json
import sys
from pathlib import Path

from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.transports.browser_json import (
    BrowserJsonTransport,
)


async def collect_all_ozon_reviews(
    adapter: OzonAdapter,
    product_url: str,
    *,
    output_path: str = "ozon_reviews.jsonl",
    start_page: int = 1,
    max_pages: int | None = None,
) -> int:
    """Потоково собирает отзывы со страниц Ozon в JSONL."""
    output = Path(output_path)
    seen_ids: set[str] = set()
    count = 0

    with output.open("w", encoding="utf-8") as file:
        async for review in adapter.iter_reviews(
            product_url=product_url,
            start_page=start_page,
            max_pages=max_pages,
        ):
            review_id = review.review_id

            if review_id and review_id in seen_ids:
                continue

            if review_id:
                seen_ids.add(review_id)

            record = {
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

            file.write(
                json.dumps(
                    record,
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


async def main() -> None:
    print(
        "[deprecation] main_3.py is deprecated; "
        "use `python -m marketplace_maps_parser` instead.",
        file=sys.stderr,
    )
    product_url = (
        "https://www.ozon.ru/product/ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    )

    transport = BrowserJsonTransport(
        timeout_ms=30_000,
        settle_ms=2_000,
        debug_dir="debug_ozon",
        humanize=True,
    )

    adapter = OzonAdapter(
        browser_transport=transport,
    )

    count = await collect_all_ozon_reviews(
        adapter=adapter,
        product_url=product_url,
        output_path="ozon_reviews.jsonl",
        start_page=1,
        max_pages=None,
    )

    print(f"Сбор завершён. Всего отзывов: {count}")


if __name__ == "__main__":
    asyncio.run(main())
