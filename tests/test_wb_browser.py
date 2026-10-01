"""Browser behavior via an in-memory Playwright double."""
from __future__ import annotations

from typing import Any

import pytest

from infrastructure.transports import wb_browser

PRODUCT = 'https://www.wildberries.ru/catalog/12345678/detail.aspx'


class Locator:
    def __init__(self, name: str) -> None:
        self.name = name
        self.first = self

    async def count(self) -> int:
        return 1

    async def inner_text(self) -> str:
        return 'A product'

    async def click(self) -> None:
        return None

    async def get_attribute(self, name: str) -> str | None:
        return None

    async def wait_for(self, **kwargs: Any) -> None:
        return None

    def nth(self, index: int) -> Locator:
        return self

    async def scroll_into_view_if_needed(self) -> None:
        return None

    @property
    def last(self) -> Locator:
        return self


class Page:
    url = PRODUCT

    def __init__(self) -> None:
        self.goto_urls: list[str] = []
        self.evaluations: list[str] = []
        self.snapshots = [
            {'cards': [{'author': 'Иван', 'text': 'Ок'}],
             'total': 2, 'average': 4.5},
            {'cards': [{'author': 'Иван', 'text': 'Ок'},
                       {'author': 'Мария', 'text': 'Да'}],
             'total': 2, 'average': 4.5},
        ]

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.url = url
        self.goto_urls.append(url)

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def title(self) -> str:
        return 'Отзывы на A product в интернет‑магазине Wildberries.ru'

    def locator(self, name: str) -> Locator:
        return Locator(name)

    async def evaluate(self, script: str) -> dict[str, Any] | None:
        self.evaluations.append(script)
        if script == wb_browser._READ_PAGE_JS:
            return self.snapshots.pop(0) if self.snapshots else {
                'cards': [], 'total': 2, 'average': 4.5,
            }
        return None


class Browser:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def new_page(self) -> Page:
        return self.page


class Context:
    def __init__(self, page: Page, **kwargs: Any) -> None:
        self.page = page

    async def __aenter__(self) -> Browser:
        return Browser(self.page)

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_browser_reads_dom_and_scrolls_without_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Page()
    monkeypatch.setattr(
        wb_browser, 'import_invisible_playwright',
        lambda: lambda **kwargs: Context(page, **kwargs),
    )
    async with wb_browser.WildberriesBrowserTransport(
        product_url=PRODUCT, settle_ms=0, max_scrolls=4,
    ) as transport:
        batches = [batch async for batch in
                   transport.iter_review_batches(PRODUCT)]
        assert transport.last_product_title == 'A product'
        assert transport.last_total_count == 2
        assert transport.last_average_rating == 4.5
    assert [len(batch) for batch in batches] == [1, 1]
    assert page.goto_urls == [
        PRODUCT,
        'https://www.wildberries.ru/catalog/12345678/feedbacks',
    ]
    assert all('fetch(' not in script for script in page.evaluations)
    assert 'card.wb.ru' not in wb_browser._READ_PAGE_JS
