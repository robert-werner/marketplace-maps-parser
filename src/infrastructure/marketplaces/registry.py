# src/infrastructure/marketplaces/registry.py
from __future__ import annotations

from collections.abc import Callable

from infrastructure.marketplaces.base import MarketplaceAdapter


class MarketplaceRegistry:
    def __init__(self) -> None:
        self._factories: dict[
            str,
            Callable[[], MarketplaceAdapter],
        ] = {}

    def register(
        self,
        name: str,
        factory: Callable[[], MarketplaceAdapter],
    ) -> None:
        self._factories[name] = factory

    def create(self, name: str) -> MarketplaceAdapter:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._factories))
            raise ValueError(
                f"Неизвестный источник {name!r}. "
                f"Доступны: {available}",
            ) from exc

        return factory()