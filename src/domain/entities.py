# src/domain/entities.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ProductRef:
    marketplace: str
    source_url: str
    product_id: str
    parent_id: str | None = None


@dataclass(slots=True)
class Review:
    review_id: str | None
    product: ProductRef
    rating: int | float | None
    text: str | None
    pros: str | None = None
    cons: str | None = None
    author: str | None = None
    created_at: datetime | None = None
    seller_answer: str | None = None
    photos: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewPage:
    product: ProductRef
    reviews: list[Review]
    total_count: int | None = None
    average_rating: float | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        invalid = [
            item
            for item in self.reviews
            if not isinstance(item, Review)
        ]

        if invalid:
            raise TypeError(
                "ReviewPage.reviews содержит элементы "
                "не типа Review"
            )