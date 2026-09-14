"""Tests for the MarketplaceRegistry factory."""
from __future__ import annotations

import pytest

from infrastructure.marketplaces.base import MarketplaceAdapter
from infrastructure.marketplaces.registry import (
    MarketplaceRegistry,
)


class FakeAdapter(MarketplaceAdapter):
    name = "fake"

    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def collect(self, product_url: str):
        raise NotImplementedError


def test_registry_register_and_create() -> None:
    registry = MarketplaceRegistry()
    registry.register(
        "fake",
        lambda: FakeAdapter("v1"),
    )

    adapter = registry.create("fake")
    assert isinstance(adapter, FakeAdapter)
    assert adapter.tag == "v1"


def test_registry_create_unknown_raises_with_available() -> None:
    registry = MarketplaceRegistry()
    registry.register("ozon", lambda: FakeAdapter("oz"))
    registry.register("wildberries", lambda: FakeAdapter("wb"))

    with pytest.raises(ValueError) as exc:
        registry.create("yandex")

    msg = str(exc.value)
    assert "yandex" in msg
    assert "ozon" in msg
    assert "wildberries" in msg


def test_registry_factory_is_called_each_time() -> None:
    """create() must return a fresh instance on every call (factory semantics)."""
    registry = MarketplaceRegistry()
    registry.register("fake", lambda: FakeAdapter("fresh"))

    a = registry.create("fake")
    b = registry.create("fake")
    assert a is not b
