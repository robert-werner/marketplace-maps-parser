# src/infrastructure/marketplaces/base.py
from __future__ import annotations

from abc import ABC, abstractmethod

from shared.entities import ReviewPage


class MarketplaceAdapter(ABC):
    name: str

    @abstractmethod
    async def collect(self, product_url: str) -> ReviewPage:
        raise NotImplementedError