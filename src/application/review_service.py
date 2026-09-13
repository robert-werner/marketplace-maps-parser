# src/application/review_service.py
from __future__ import annotations

from domain.entities import ReviewPage
from infrastructure.marketplaces.registry import MarketplaceRegistry


class ReviewService:
    def __init__(self, registry: MarketplaceRegistry) -> None:
        self.registry = registry

    async def collect(
        self,
        marketplace: str,
        product_url: str,
    ) -> ReviewPage:
        adapter = self.registry.create(marketplace)
        return await adapter.collect(product_url)