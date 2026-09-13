# src/infrastructure/repositories/jsonl_repository.py
from __future__ import annotations

import json
from pathlib import Path

from domain.entities import Review


class JsonlReviewRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def append(self, review: Review) -> None:
        record = {
            "review_id": review.review_id,
            "marketplace": review.product.marketplace,
            "product_id": review.product.product_id,
            "rating": review.rating,
            "text": review.text,
            "author": review.author,
            "created_at": review.created_at,
            "seller_answer": review.seller_answer,
            "raw": review.raw,
        }

        with self.path.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )