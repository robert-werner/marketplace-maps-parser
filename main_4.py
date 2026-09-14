from __future__ import annotations

import asyncio
import json
from pathlib import Path

from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.transports.browser_json import BrowserJsonTransport


async def collect_all_ozon_reviews(
    adapter: OzonAdapter,
    product_url: str,
    *,
    output_path: str = "ozon_reviews.jsonl",
    start_page: int = 1,
    max_pages: int | None = None,
) -> int:
    """Потоково сохраняет все найденные отзывы Ozon в JSONL."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    count = 0

    with output.open("w", encoding="utf-8") as file:
        async for review in adapter.iter_reviews(
            product_url=product_url,
            start_page=start_page,
            max_pages=max_pages,
        ):
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
    product_url = (
        "https://www.ozon.ru/product/"
        "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
    )

    transport = BrowserJsonTransport(
        timeout_ms=90_000,
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

