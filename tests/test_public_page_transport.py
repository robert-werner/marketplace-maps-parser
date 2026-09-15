"""Tests for the PublicPageTransport.

The PublicPageTransport scrapes the public review page DOM
(``/product/<id>/reviews?page=N``) instead of hitting the internal
API endpoint. These tests stub the invisible-playwright browser
and the DOM card reader so they don't need a real browser or
network.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from infrastructure.transports import public_page as pp_module
from infrastructure.transports.public_page import PublicPageTransport


# Cache the real asyncio.sleep so test monkeypatches can call it
# without infinite recursion.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(*args, **kwargs):
    return None


# ---------------------------------------------------------------------------
# Fake browser / page / locator / response
# ---------------------------------------------------------------------------


class _FakeLocator:
    """Stand-in for a Playwright Locator with a fixed set of
    matching elements (cards).

    The ``cards`` parameter is a callable that returns the current
    list of cards. This allows scroll-mode tests to grow the card
    list on wheel events and have ``count()`` reflect the change on
    the next call.
    """

    def __init__(self, cards_getter) -> None:
        # ``cards_getter`` is a callable () -> list[dict] so the
        # locator can re-evaluate on every count() call (mirrors
        # how real Playwright re-evaluates the DOM).
        self._cards_getter = cards_getter

    async def count(self) -> int:
        return len(self._cards_getter())

    @property
    def first(self) -> "_FakeFirstCard":
        if not self._cards_getter():
            # Return a _FakeFirstCard that raises wait_for to simulate
            # the "no cards" case
            return _FakeFirstCard(has_card=False)
        return _FakeFirstCard(has_card=True)

    def nth(self, index: int) -> "_FakeCardLocator":
        cards = self._cards_getter()
        if index >= len(cards):
            raise IndexError(f"card index {index} out of range")
        return _FakeCardLocator(cards[index])


class _FakeFirstCard:
    """Stand-in for ``locator.first`` — used only for
    ``wait_for(state="attached")``."""

    def __init__(self, *, has_card: bool) -> None:
        self.has_card = has_card

    async def wait_for(self, *, state: str, timeout: int) -> None:
        if not self.has_card:
            raise RuntimeError(
                f"wait_for({state}) timed out — no cards"
            )
        # Otherwise return immediately (cards are "attached").


class _FakeCardLocator:
    """Stand-in for a single card Locator."""

    def __init__(self, card: dict[str, Any]) -> None:
        self._card = card

    async def get_attribute(self, name: str) -> str | None:
        attr_map = {
            "data-review-uuid": self._card.get("uuid"),
            "publishedat": self._card.get("published_at"),
            "statusid": self._card.get("status_id"),
        }
        return attr_map.get(name)

    async def inner_text(self) -> str:
        return self._card.get("text", "")

    def locator(self, selector: str) -> "_FakeStarOrImageLocator":
        if "rpProducta9c" in selector:
            # Rating container
            return _FakeStarOrImageLocator(
                kind="stars",
                items=self._card.get("stars", []),
            )
        if selector == "img":
            return _FakeStarOrImageLocator(
                kind="images",
                items=self._card.get("images", []),
            )
        return _FakeStarOrImageLocator(kind="empty", items=[])

    async def evaluate(self, expression: str, *args) -> Any:
        # Pretend to evaluate the star-color JS — return a fixed
        # color for filled stars, a different color for empty.
        return self._card.get("_star_color", {
            "elementColor": "rgb(0, 0, 0)",
            "pathFill": "rgb(255, 168, 0)",  # yellow
            "pathAttribute": "fill",
            "className": "filled",
        })


class _FakeStarOrImageLocator:
    """Stand-in for the SVG-stars or img locator."""

    def __init__(self, *, kind: str, items: list[Any]) -> None:
        self.kind = kind
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> "_FakeStarOrImageItemLocator":
        return _FakeStarOrImageItemLocator(
            item=self.items[index],
            kind=self.kind,
        )

    def locator(self, selector: str) -> "_FakeStarOrImageLocator":
        # Nested locators (e.g. svg inside rating container) — return
        # the same set of items. The transport calls
        # ``rating_container.locator("svg")`` to get the stars.
        return self


class _FakeStarOrImageItemLocator:
    """Stand-in for one SVG star or one img element."""

    def __init__(self, *, item: Any, kind: str) -> None:
        self.item = item
        self.kind = kind

    async def get_attribute(self, name: str) -> str | None:
        if self.kind == "images" and name == "src":
            return self.item
        return None

    async def evaluate(self, expression: str, *args) -> Any:
        # For stars: return the color dict from the item.
        if self.kind == "stars":
            return self.item  # a color dict
        return None


class _FakePage:
    """Stand-in for a Playwright Page."""

    def __init__(
        self,
        *,
        cards_by_page: dict[int, list[dict[str, Any]]],
    ) -> None:
        self.cards_by_page = cards_by_page
        self.current_page_num = 0
        self.goto_calls: list[str] = []
        self.add_init_script_calls: list[str] = []
        self.screenshot_calls: list[str] = []
        self.mouse_wheel_calls: list[int] = []
        # Cached _FakeMouse — created lazily and reused so that
        # ``page.mouse.on_wheel_callback = ...`` (set by the test)
        # persists across accesses.
        self._mouse: "_FakeMouse | None" = None

    async def add_init_script(self, script: str) -> None:
        self.add_init_script_calls.append(script)

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)
        # Extract page number from URL
        # /product/foo-123/reviews?page=N
        if "page=" in url:
            try:
                self.current_page_num = int(
                    url.split("page=")[1].split("&")[0]
                )
            except (ValueError, IndexError):
                self.current_page_num = 1
        else:
            self.current_page_num = 1

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def wait_for(self, *args, **kwargs) -> None:
        return None

    def locator(self, selector: str) -> _FakeLocator:
        if selector == "[data-review-uuid]":
            # Return a locator that re-evaluates on every count()
            # call — mirrors how real Playwright works (each
            # count() / nth() call hits the live DOM).
            return _FakeLocator(
                lambda: self.cards_by_page.get(self.current_page_num, []),
            )
        return _FakeLocator(lambda: [])

    async def screenshot(self, *args, **kwargs) -> None:
        self.screenshot_calls.append("screenshot")

    @property
    def mouse(self) -> "_FakeMouse":
        # Reuse the same _FakeMouse instance so test callbacks
        # set via ``page.mouse.on_wheel_callback = ...`` persist.
        if self._mouse is None:
            self._mouse = _FakeMouse(self)
        return self._mouse

    async def content(self) -> str:
        return f"<html>page {self.current_page_num}</html>"

    async def close(self) -> None:
        pass


class _FakeMouse:
    """Stand-in for ``page.mouse`` — records wheel calls and
    triggers a callback so the test can simulate lazy-load."""

    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.on_wheel_callback = None

    async def wheel(self, x: int, y: int) -> None:
        self._page.mouse_wheel_calls.append(y)
        # Allow test to hook in and update the cards state
        if self.on_wheel_callback is not None:
            self.on_wheel_callback()


class _FakeBrowser:
    """Stand-in for invisible-playwright browser."""

    def __init__(self, *, cards_by_page: dict[int, list[dict[str, Any]]]) -> None:
        self.cards_by_page = cards_by_page
        self.pages_created: list[_FakePage] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def new_page(self) -> _FakePage:
        page = _FakePage(cards_by_page=self.cards_by_page)
        self.pages_created.append(page)
        return page


def _patch_browser(monkeypatch, cards_by_page):
    """Patch ``_import_invisible_playwright`` to return a fake
    browser factory."""
    fake_browser = _FakeBrowser(cards_by_page=cards_by_page)

    def fake_import():
        def factory(**kwargs):
            return fake_browser
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )
    return fake_browser


def _make_card(
    uuid: str,
    *,
    rating: int = 5,
    text: str = "Отличный товар",
    author: str = "Иван",
    published_at: str = "1700000000",
) -> dict[str, Any]:
    """Build a fake card dict that _read_review_cards will parse."""
    full_text = f"АБ\n{author}\n5 октября 2023\n{text}\nВам помог этот отзыв?\nДа 5 Нет 1"
    stars = [
        # 5 filled stars
        {"elementColor": "rgb(0,0,0)", "pathFill": "rgb(255, 168, 0)", "pathAttribute": "fill", "className": "filled"},
        {"elementColor": "rgb(0,0,0)", "pathFill": "rgb(255, 168, 0)", "pathAttribute": "fill", "className": "filled"},
        {"elementColor": "rgb(0,0,0)", "pathFill": "rgb(255, 168, 0)", "pathAttribute": "fill", "className": "filled"},
        {"elementColor": "rgb(0,0,0)", "pathFill": "rgb(255, 168, 0)", "pathAttribute": "fill", "className": "filled"},
        {"elementColor": "rgb(0,0,0)", "pathFill": "rgb(255, 168, 0)", "pathAttribute": "fill", "className": "filled"},
    ][:rating]
    return {
        "uuid": uuid,
        "text": full_text,
        "published_at": published_at,
        "status_id": "2",
        "rating": rating,
        "stars": stars,
        "images": [],
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_transport_defaults():
    t = PublicPageTransport()
    assert t.timeout_ms == 90_000
    assert t.settle_ms == 3_000
    assert t.stealth is True
    assert t.max_idle_pages == 2
    assert t.scroll_max_idle_rounds == 5


def test_transport_custom_args():
    t = PublicPageTransport(
        timeout_ms=60_000,
        settle_ms=5_000,
        max_idle_pages=3,
        scroll_max_idle_rounds=10,
    )
    assert t.timeout_ms == 60_000
    assert t.settle_ms == 5_000
    assert t.max_idle_pages == 3
    assert t.scroll_max_idle_rounds == 10


# ---------------------------------------------------------------------------
# iter_ozon_reviews_json — pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_paginates_through_pages(monkeypatch):
    """The iterator should fetch /reviews?page=1, then page=2, etc.,
    yielding each page's payload.
    """
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [_make_card("r3"), _make_card("r4")],
        # Page 3 has no cards — should stop after this
        3: [],
    }
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    pages_yielded = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages_yielded.append((page_num, payload))

    # Page 1 has cards, page 2 has cards, page 3 has 0 cards →
    # stop after page 3 (max_idle_pages=1).
    assert len(pages_yielded) == 2  # only pages 1 and 2 yielded
    assert pages_yielded[0][0] == 1
    assert pages_yielded[1][0] == 2
    # Confirm we navigated to pages 1, 2, 3
    assert len(fake_browser.pages_created[0].goto_calls) == 3


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_dedupes_across_pages(monkeypatch):
    """If the same UUID appears on multiple pages (e.g. Ozon
    returns the same first card on every page), it should only be
    yielded once.
    """
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [_make_card("r1"), _make_card("r3")],  # r1 is a dup
        3: [],  # stop here
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    all_reviews = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        all_reviews.extend(payload.get("reviews", []))

    # 3 unique reviews: r1, r2, r3
    ids = [r["reviewId"] for r in all_reviews]
    assert ids == ["r1", "r2", "r3"]


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_stops_on_empty_page(monkeypatch):
    """When a page has 0 review cards, the iterator should count
    it as an idle page and stop after ``max_idle_pages`` consecutive
    idle pages.
    """
    cards_by_page = {
        1: [_make_card("r1")],  # 1 review
        2: [],  # idle 1
        3: [],  # idle 2 → stop (max_idle_pages=2)
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=2)
    pages = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    # Only page 1 yielded (pages 2 and 3 are idle → stop)
    assert pages == [1]


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_respects_max_pages(monkeypatch):
    """``max_pages=2`` should stop after 2 pages, even if there
    are more.
    """
    cards_by_page = {
        1: [_make_card("r1")],
        2: [_make_card("r2")],
        3: [_make_card("r3")],  # should NOT be fetched
    }
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=10)
    pages = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        max_pages=2,
        retry_attempts=1,
    ):
        pages.append(page_num)

    assert pages == [1, 2]
    # Confirm page 3 was NOT fetched
    assert len(fake_browser.pages_created[0].goto_calls) == 2


@pytest.mark.asyncio
async def test_iter_ozon_reviews_json_payload_shape(monkeypatch):
    """The yielded payload should have ``reviews`` and ``nextPage``
    keys so the existing adapter code can consume it.
    """
    cards_by_page = {
        1: [_make_card("r1", rating=5)],
        2: [],  # stop
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        assert "reviews" in payload
        assert "nextPage" in payload
        assert isinstance(payload["reviews"], list)
        assert len(payload["reviews"]) == 1
        review = payload["reviews"][0]
        assert review["reviewId"] == "r1"
        assert review["rating"] == 5
        break


# ---------------------------------------------------------------------------
# iter_ozon_reviews_by_scroll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_ozon_reviews_by_scroll_yields_cards(monkeypatch):
    """Scroll mode should yield batches of new cards as they
    appear during scrolling.
    """
    # Page 1 has 2 cards initially; after scroll, 1 more card appears.
    initial_cards = [_make_card("r1"), _make_card("r2")]
    scrolled_cards = [_make_card("r1"), _make_card("r2"), _make_card("r3")]

    fake_browser = _FakeBrowser(cards_by_page={1: initial_cards})

    async def new_page():
        page = _FakePage(cards_by_page={1: list(initial_cards)})
        fake_browser.pages_created.append(page)

        # Hook: when scroll is called, grow the cards list
        def on_wheel():
            page.cards_by_page[1] = list(scrolled_cards)

        page.mouse.on_wheel_callback = on_wheel
        return page

    fake_browser.new_page = new_page

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright",
        lambda: lambda **kw: fake_browser,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0,
        scroll_max_idle_rounds=3,
        scroll_pause_ms=1,
    )
    all_cards = []
    async for batch in transport.iter_ozon_reviews_by_scroll(
        product_path="/product/foo-123",
    ):
        all_cards.extend(batch)

    # First batch: r1, r2 (initial)
    # After scroll: r1, r2 (already seen), r3 (new)
    # So we should see r1, r2, then r3.
    uuids = [c["uuid"] for c in all_cards]
    assert "r1" in uuids
    assert "r2" in uuids
    assert "r3" in uuids


# ---------------------------------------------------------------------------
# Stealth init script applied to new pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stealth_init_script_applied_to_new_page(monkeypatch):
    """When stealth=True, ``page.add_init_script`` should be
    called with the stealth script.
    """
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, stealth=True, max_idle_pages=1)
    async for _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pass

    assert len(fake_browser.pages_created) > 0
    page = fake_browser.pages_created[0]
    assert len(page.add_init_script_calls) == 1
    # The applied script should be the stealth init script
    assert "navigator" in page.add_init_script_calls[0]
    assert "webdriver" in page.add_init_script_calls[0]


@pytest.mark.asyncio
async def test_stealth_not_applied_when_disabled(monkeypatch):
    """When stealth=False, no init script should be added."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, stealth=False, max_idle_pages=1)
    async for _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pass

    page = fake_browser.pages_created[0]
    assert len(page.add_init_script_calls) == 0


# ---------------------------------------------------------------------------
# iter_all_ozon_reviews — pagination + scroll supplement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_ozon_reviews_yields_pagination_strategy(monkeypatch):
    """The unified iterator should yield ``("pagination", node)``
    tuples from the pagination phase.
    """
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [],  # stop
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    yielded = []
    async for strategy, node in transport.iter_all_ozon_reviews(
        product_path="/product/foo-123",
        retry_attempts=1,
        page_delay_seconds=0,
        scroll_pause_seconds=0,
    ):
        yielded.append((strategy, node.get("reviewId")))

    # All yields should be "pagination"
    assert all(s == "pagination" for s, _ in yielded)
    ids = [rid for _, rid in yielded]
    assert ids == ["r1", "r2"]


@pytest.mark.asyncio
async def test_iter_all_ozon_reviews_max_reviews_cap(monkeypatch):
    """``max_reviews`` should stop the unified iterator after the
    cap is reached.
    """
    cards_by_page = {
        1: [_make_card(f"r{i}") for i in range(10)],
        2: [],  # stop
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    yielded = []
    async for strategy, node in transport.iter_all_ozon_reviews(
        product_path="/product/foo-123",
        max_reviews=3,
        retry_attempts=1,
        page_delay_seconds=0,
        scroll_pause_seconds=0,
    ):
        yielded.append(node.get("reviewId"))

    assert len(yielded) == 3
    assert yielded == ["r0", "r1", "r2"]


# ---------------------------------------------------------------------------
# get_ozon_reviews_json (single-page)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ozon_reviews_json_returns_first_page(monkeypatch):
    """The single-page fetch should return the payload for the
    requested page number.
    """
    cards_by_page = {
        1: [_make_card("r1")],
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    payload = await transport.get_ozon_reviews_json(
        product_path="/product/foo-123",
        page_number=1,
    )

    assert "reviews" in payload
    assert len(payload["reviews"]) == 1
    assert payload["reviews"][0]["reviewId"] == "r1"


# ---------------------------------------------------------------------------
# _read_review_cards — DOM parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_review_cards_extracts_uuid_and_attributes():
    """The card reader should extract uuid, published_at, status_id,
    text, rating, and images from the DOM.
    """
    transport = PublicPageTransport()
    cards = [_make_card("r1", rating=4)]
    locator = _FakeLocator(lambda: cards)

    result = await transport._read_review_cards(locator)

    assert len(result) == 1
    card = result[0]
    assert card["uuid"] == "r1"
    assert card["published_at"] == "1700000000"
    assert card["status_id"] == "2"
    assert isinstance(card["text"], str)
    assert "Отличный товар" in card["text"]
    # Rating should be 4 (we set 4 filled stars)
    assert card["rating"] == 4


@pytest.mark.asyncio
async def test_read_review_cards_handles_empty_locator():
    """An empty locator (no cards) should produce an empty list."""
    transport = PublicPageTransport()
    locator = _FakeLocator(lambda: [])

    result = await transport._read_review_cards(locator)

    assert result == []


# ---------------------------------------------------------------------------
# _is_filled_star heuristic
# ---------------------------------------------------------------------------


def test_is_filled_star_recognizes_yellow_star():
    color = {
        "elementColor": "rgb(0, 0, 0)",
        "pathFill": "rgb(255, 168, 0)",  # yellow
        "pathAttribute": "fill",
        "className": "filled",
    }
    assert PublicPageTransport._is_filled_star(color)


def test_is_filled_star_recognizes_no_color():
    assert not PublicPageTransport._is_filled_star(None)
    assert not PublicPageTransport._is_filled_star({})


# ---------------------------------------------------------------------------
# close() — no-op for API compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_noop():
    """close() should not raise — it's a no-op for this transport
    (browser sessions are scoped to each iterator call).
    """
    transport = PublicPageTransport()
    await transport.close()  # should not raise


# ---------------------------------------------------------------------------
# randomize_fingerprint — fresh browser per page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_randomize_fingerprint_creates_new_browser_per_page(monkeypatch):
    """When randomize_fingerprint=True, a new InvisiblePlaywright
    browser should be created for each page (each with a new random
    fingerprint via seed=None).
    """
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [_make_card("r3"), _make_card("r4")],
        3: [],  # stop after this
    }

    # Track how many browser instances are created
    browser_create_count = {"n": 0}
    browsers_created: list[_FakeBrowser] = []

    def fake_import():
        def factory(**kwargs):
            browser_create_count["n"] += 1
            # Each call creates a NEW fake browser instance
            b = _FakeBrowser(cards_by_page=cards_by_page)
            browsers_created.append(b)
            return b
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=True,
    )
    pages = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    # 2 pages yielded (page 3 is empty → stop)
    assert pages == [1, 2]
    # With randomize_fingerprint=True, a new browser should be
    # created for each fetched page (including the empty page 3
    # which triggers the idle-pages stop) → 3 browser instances.
    assert browser_create_count["n"] == 3


@pytest.mark.asyncio
async def test_randomize_fingerprint_passes_seed_none(monkeypatch):
    """Each browser created in randomize_fingerprint mode should
    be passed seed=None (so invisible-playwright generates a random
    fingerprint via secrets.randbits(31)).
    """
    cards_by_page = {
        1: [_make_card("r1")],
        2: [],  # stop
    }

    received_seeds: list = []

    def fake_import():
        def factory(**kwargs):
            received_seeds.append(kwargs.get("seed"))
            return _FakeBrowser(cards_by_page=cards_by_page)
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=True,
    )
    async for _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pass

    # At least one browser created, all with seed=None
    assert len(received_seeds) >= 1
    assert all(s is None for s in received_seeds)


@pytest.mark.asyncio
async def test_no_randomize_fingerprint_uses_single_browser(monkeypatch):
    """When randomize_fingerprint=False (default), only ONE
    InvisiblePlaywright browser should be created for the whole
    pagination run.
    """
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [_make_card("r3")],
        3: [],  # stop
    }

    browser_create_count = {"n": 0}

    def fake_import():
        def factory(**kwargs):
            browser_create_count["n"] += 1
            return _FakeBrowser(cards_by_page=cards_by_page)
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=False,  # default
    )
    pages = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    assert pages == [1, 2]
    # Only ONE browser for the whole run
    assert browser_create_count["n"] == 1


@pytest.mark.asyncio
async def test_randomize_fingerprint_dedup_across_pages(monkeypatch):
    """Even with randomize_fingerprint=True (new browser per page),
    UUID dedup should still work — the seen_uuids set is shared
    across the whole pagination run.
    """
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [_make_card("r1"), _make_card("r3")],  # r1 is a dup
        3: [],  # stop
    }

    def fake_import():
        def factory(**kwargs):
            return _FakeBrowser(cards_by_page=cards_by_page)
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=True,
    )
    all_reviews = []
    async for page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        all_reviews.extend(payload.get("reviews", []))

    # 3 unique reviews: r1, r2, r3
    ids = [r["reviewId"] for r in all_reviews]
    assert ids == ["r1", "r2", "r3"]
