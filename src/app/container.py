# src/app/container.py
from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.marketplaces.registry import MarketplaceRegistry
from infrastructure.marketplaces.wildberries import WildberriesAdapter
from infrastructure.transports.browser_dom import BrowserDomTransport
from infrastructure.transports.http import HttpJsonTransport


def build_registry(
    http_transport: HttpJsonTransport,
    browser_dom_transport: BrowserDomTransport,
) -> MarketplaceRegistry:
    registry = MarketplaceRegistry()

    registry.register(
        "wildberries",
        lambda: WildberriesAdapter(http_transport),
    )

    registry.register(
        "ozon",
        lambda: OzonAdapter(browser_dom_transport),
    )

    return registry