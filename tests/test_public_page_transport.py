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


class _FakeNextButtonLocator:
    """Stand-in for the reviews widget's «Дальше» control.

    Visible while the NEXT page number in ``cards_by_page`` has
    cards; clicking advances the fake page's number (the widget
    replaces its content rather than appending)."""

    def __init__(self, *, page: _FakePage) -> None:
        self._page = page

    def _next_has_cards(self) -> bool:
        nxt = self._page.cards_by_page.get(
            self._page.current_page_num + 1, [],
        )
        return bool(nxt) and not self._page._in_challenge

    async def count(self) -> int:
        return 1 if self._next_has_cards() else 0

    @property
    def first(self) -> _FakeNextButtonLocator:
        return self

    async def click(self, **kwargs) -> None:
        if self._next_has_cards():
            self._page.current_page_num += 1


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
    def first(self) -> _FakeFirstCard:
        if not self._cards_getter():
            # Return a _FakeFirstCard that raises wait_for to simulate
            # the "no cards" case
            return _FakeFirstCard(has_card=False)
        return _FakeFirstCard(has_card=True)

    def nth(self, index: int) -> _FakeCardLocator:
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

    def locator(self, selector: str) -> _FakeStarOrImageLocator:
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
        # The rating extractor runs one JS pass over the whole card
        # and returns the count of orange stars — the fake returns
        # the number of "stars" the fixture provides (= rating).
        if "byGlyph" in expression:
            return len(self._card.get("stars", [])) or None
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

    def nth(self, index: int) -> _FakeStarOrImageItemLocator:
        return _FakeStarOrImageItemLocator(
            item=self.items[index],
            kind=self.kind,
        )

    def locator(self, selector: str) -> _FakeStarOrImageLocator:
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


class _FakeCookieContext:
    """Records add_cookies calls (stand-in for page.context)."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.added.extend(cookies)


class _FakePage:
    """Stand-in for a Playwright Page.

    ``challenge_until_goto=N`` (set on the owning browser) simulates
    Ozon's antibot: the first N ``goto`` calls — across ALL pages of
    that browser — land on a challenge page (antibot title, no
    cards); the challenge clears on the next navigation. The
    counter lives on the browser because the per-page (proxy-pool)
    mode creates a fresh page per fetch while the challenge state
    belongs to the exit IP / session.
    """

    _ANTIBOT_TITLE = "Antibot Challenge Page"
    _NORMAL_TITLE = "Отзывы о товаре — OZON"

    def __init__(
        self,
        *,
        cards_by_page: dict[int, list[dict[str, Any]]],
        browser: _FakeBrowser,
    ) -> None:
        self.cards_by_page = cards_by_page
        self._browser = browser
        self.current_page_num = 0
        self.goto_calls: list[str] = []
        self.add_init_script_calls: list[str] = []
        self.screenshot_calls: list[str] = []
        self.mouse_wheel_calls: list[int] = []
        # Cached _FakeMouse — created lazily and reused so that
        # ``page.mouse.on_wheel_callback = ...`` (set by the test)
        # persists across accesses.
        self._mouse: _FakeMouse | None = None
        self._cookie_context = _FakeCookieContext()

    @property
    def context(self) -> _FakeCookieContext:
        return self._cookie_context

    @property
    def _in_challenge(self) -> bool:
        return (
            self._browser.total_gotos
            <= self._browser.challenge_until_goto
        )

    async def title(self) -> str:
        if self._in_challenge:
            return self._ANTIBOT_TITLE
        return self._NORMAL_TITLE

    async def add_init_script(self, script: str) -> None:
        self.add_init_script_calls.append(script)

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)
        self._browser.total_gotos += 1
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

    @property
    def url(self) -> str:
        return (
            "https://www.ozon.ru/product/foo-123/reviews"
            f"?page={max(self.current_page_num, 1)}&page_key=fake"
        )

    def locator(self, selector: str):
        if selector == "[data-review-uuid]":
            # Return a locator that re-evaluates on every count()
            # call — mirrors how real Playwright works (each
            # count() / nth() call hits the live DOM). While the
            # antibot challenge is active, no cards are rendered.
            def _cards() -> list[dict[str, Any]]:
                if self._in_challenge:
                    return []
                return self.cards_by_page.get(
                    self.current_page_num, [],
                )

            return _FakeLocator(_cards)
        if "Дальше" in selector:
            return _FakeNextButtonLocator(page=self)
        return _FakeLocator(lambda: [])

    async def screenshot(self, *args, **kwargs) -> None:
        self.screenshot_calls.append("screenshot")

    @property
    def mouse(self) -> _FakeMouse:
        # Reuse the same _FakeMouse instance so test callbacks
        # set via ``page.mouse.on_wheel_callback = ...`` persist.
        if self._mouse is None:
            self._mouse = _FakeMouse(self)
        return self._mouse

    async def content(self) -> str:
        if self._in_challenge:
            return "<html>antibot challenge __cf_chl script</html>"
        return f"<html>page {self.current_page_num}</html>"

    async def evaluate(self, expression: str, *args) -> Any:
        # Fast batch readers of the widget flow (one round-trip per
        # page instead of ~240 attribute reads).
        if "querySelectorAll('[data-review-uuid]')" not in expression:
            return None
        cards = (
            []
            if self._in_challenge
            else self.cards_by_page.get(self.current_page_num, [])
        )
        if ".map(e => e.getAttribute('data-review-uuid'))" in expression:
            return [c.get("uuid") for c in cards]
        return [
            {
                "uuid": c.get("uuid"),
                "published_at": c.get("published_at"),
                "status_id": c.get("status_id"),
                "text": c.get("text"),
                "rating": c.get("rating"),
                "images": c.get("images", []),
            }
            for c in cards
        ]

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

    def __init__(
        self,
        *,
        cards_by_page: dict[int, list[dict[str, Any]]],
        challenge_until_goto: int = 0,
    ) -> None:
        self.cards_by_page = cards_by_page
        self.challenge_until_goto = challenge_until_goto
        self.total_gotos = 0
        self.pages_created: list[_FakePage] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def new_page(self) -> _FakePage:
        page = _FakePage(
            cards_by_page=self.cards_by_page,
            browser=self,
        )
        self.pages_created.append(page)
        return page


def _patch_browser(
    monkeypatch,
    cards_by_page,
    challenge_until_goto: int = 0,
):
    """Patch ``_import_invisible_playwright`` to return a fake
    browser factory."""
    fake_browser = _FakeBrowser(
        cards_by_page=cards_by_page,
        challenge_until_goto=challenge_until_goto,
    )

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
    full_text = (
        f"АБ\n{author}\n5 октября 2023\n{text}\n"
        "Вам помог этот отзыв?\nДа 5 Нет 1"
    )
    stars = [
        # 5 filled stars
        {
            "elementColor": "rgb(0,0,0)",
            "pathFill": "rgb(255, 168, 0)",
            "pathAttribute": "fill",
            "className": "filled",
        },
        {
            "elementColor": "rgb(0,0,0)",
            "pathFill": "rgb(255, 168, 0)",
            "pathAttribute": "fill",
            "className": "filled",
        },
        {
            "elementColor": "rgb(0,0,0)",
            "pathFill": "rgb(255, 168, 0)",
            "pathAttribute": "fill",
            "className": "filled",
        },
        {
            "elementColor": "rgb(0,0,0)",
            "pathFill": "rgb(255, 168, 0)",
            "pathAttribute": "fill",
            "className": "filled",
        },
        {
            "elementColor": "rgb(0,0,0)",
            "pathFill": "rgb(255, 168, 0)",
            "pathAttribute": "fill",
            "className": "filled",
        },
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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
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
    # Confirm we navigated: 1 warmup (product page) + pages 1, 2, 3
    assert len(fake_browser.pages_created[0].goto_calls) == 4


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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
    all_reviews = []
    async for _page_num, payload in transport.iter_ozon_reviews_json(
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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=2
    )
    pages = []
    async for page_num, _payload in transport.iter_ozon_reviews_json(
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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=10
    )
    pages = []
    async for page_num, _payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        max_pages=2,
        retry_attempts=1,
    ):
        pages.append(page_num)

    assert pages == [1, 2]
    # Confirm page 3 was NOT fetched: 1 warmup + pages 1 and 2
    assert len(fake_browser.pages_created[0].goto_calls) == 3


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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
    async for _page_num, payload in transport.iter_ozon_reviews_json(
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
        page = _FakePage(
            cards_by_page={1: list(initial_cards)},
            browser=fake_browser,
        )
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
        settle_ms=0, lazy_wait_ms=0,
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
# Stealth init script NOT applied to new pages (measured harm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_stealth_init_script_even_when_enabled(monkeypatch):
    """No JS stealth init script is applied to public pages — not
    even with stealth=True. Measured 2026-09-15: the
    playwright-stealth-style script makes Ozon serve its «Похоже,
    нет соединения» error page (0 cards) on both engines, while
    the same session without the script gets HTTP 200 + 30 cards.
    invisible-playwright provides the real stealth itself."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, stealth=True,
        max_idle_pages=1,
    )
    async for _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pass

    assert len(fake_browser.pages_created) > 0
    page = fake_browser.pages_created[0]
    assert len(page.add_init_script_calls) == 0


@pytest.mark.asyncio
async def test_stealth_not_applied_when_disabled(monkeypatch):
    """When stealth=False, no init script should be added."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, stealth=False,
        max_idle_pages=1,
    )
    async for _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pass

    page = fake_browser.pages_created[0]
    assert len(page.add_init_script_calls) == 0


# ---------------------------------------------------------------------------
# iter_all_ozon_reviews — widget flow primary, legacy fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_all_ozon_reviews_widget_primary(monkeypatch):
    """The unified iterator's first phase is the widget flow; when
    it collects anything, the legacy pagination/scroll phases are
    skipped (they can only re-deliver the same anonymous-capped
    reviews)."""
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [],  # «Дальше» hidden (next page empty) → widget stops
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
    yielded = []
    async for strategy, node in transport.iter_all_ozon_reviews(
        product_path="/product/foo-123",
        retry_attempts=1,
        page_delay_seconds=0,
        scroll_pause_seconds=0,
    ):
        yielded.append((strategy, node.get("uuid")))

    assert all(s == "widget" for s, _ in yielded)
    ids = [rid for _, rid in yielded]
    assert ids == ["r1", "r2"]


@pytest.mark.asyncio
async def test_widget_flow_follows_next_button(monkeypatch):
    """The widget flow clicks «Дальше» and waits for the card set
    to be REPLACED (pagination-as-infinite-scroll), yielding each
    batch until the button disappears."""
    cards_by_page = {
        1: [_make_card("r1"), _make_card("r2")],
        2: [_make_card("r3")],
        3: [],  # no 3rd page → button hidden after page 2
    }
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
    batches = []
    async for batch in transport.iter_ozon_reviews_by_widget(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        batches.append([c["uuid"] for c in batch])

    assert batches == [["r1", "r2"], ["r3"]]
    # both pages were rendered in ONE session (no restarts)
    assert len(fake_browser.pages_created) == 1


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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
    yielded = []
    async for _strategy, node in transport.iter_all_ozon_reviews(
        product_path="/product/foo-123",
        max_reviews=3,
        retry_attempts=1,
        page_delay_seconds=0,
        scroll_pause_seconds=0,
    ):
        yielded.append(node.get("uuid"))

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

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
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
        settle_ms=0, lazy_wait_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=True,
    )
    pages = []
    async for page_num, _payload in transport.iter_ozon_reviews_json(
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
        settle_ms=0, lazy_wait_ms=0,
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
        settle_ms=0, lazy_wait_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=False,  # default
    )
    pages = []
    async for page_num, _payload in transport.iter_ozon_reviews_json(
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
        settle_ms=0, lazy_wait_ms=0,
        max_idle_pages=1,
        randomize_fingerprint=True,
    )
    all_reviews = []
    async for _page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        all_reviews.extend(payload.get("reviews", []))

    # 3 unique reviews: r1, r2, r3
    ids = [r["reviewId"] for r in all_reviews]
    assert ids == ["r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# Antibot detection & hardening
# ---------------------------------------------------------------------------


class _TitleOnlyPage:
    """Minimal page stub for ``_page_is_antibot`` unit tests."""

    def __init__(self, title: str, html: str = "") -> None:
        self._title = title
        self._html = html

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._html


@pytest.mark.asyncio
async def test_page_is_antibot_matches_challenge_titles():
    transport = PublicPageTransport()
    for title in (
        "Antibot Challenge Page",
        "Похоже, нет соединения",
        "Just a moment...",
        "Attention Required! | Cloudflare",
        "Доступ ограничен",
    ):
        page = _TitleOnlyPage(title)
        assert await transport._page_is_antibot(page), title


@pytest.mark.asyncio
async def test_page_is_antibot_real_review_page_not_flagged():
    """A real reviews page — including one that mentions the string
    "antibot" in its HTML (measured 2026-09 on a page that rendered
    30 cards) — must NOT be flagged."""
    transport = PublicPageTransport()
    page = _TitleOnlyPage(
        "304 отзыв на IP-телефон Yealink SIP-T30 / OZON",
        html="<html>…/antibot/… widget …</html>",
    )
    assert not await transport._page_is_antibot(page)


@pytest.mark.asyncio
async def test_page_is_antibot_matches_cloudflare_html_markers():
    transport = PublicPageTransport()
    for html in (
        "<html>Выключите VPN, перезагрузите роутер</html>",
        '<script src="/cdn-cgi/challenge-platform/h/b/or.js">',
    ):
        page = _TitleOnlyPage("", html=html)
        assert await transport._page_is_antibot(page), html[:40]


@pytest.mark.asyncio
async def test_single_browser_retries_antibot_challenge(monkeypatch):
    """When a warmed-up navigation still lands on the antibot
    challenge, the single-browser path must cool down, re-warm and
    re-fetch the SAME page instead of skipping it."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    # gotos #1 (warmup) and #2 (page 1) land on the challenge →
    # no cards; the retry warm-up + goto (#3, #4) clear the
    # challenge and the cards render.
    fake_browser = _patch_browser(
        monkeypatch, cards_by_page, challenge_until_goto=2,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1
    )
    pages = []
    async for page_num, _payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=3,
    ):
        pages.append(page_num)

    assert pages == [1]
    page = fake_browser.pages_created[0]
    # warmup + page1 + retry(warmup + page1) + page2 = 5 gotos
    assert len(page.goto_calls) == 5


@pytest.mark.asyncio
async def test_single_browser_antibot_exhaustion_counts_idle(monkeypatch):
    """When the challenge never clears, the page is treated as idle
    (same as an empty page) instead of looping forever."""
    cards_by_page = {1: [_make_card("r1")]}
    fake_browser = _patch_browser(
        monkeypatch, cards_by_page, challenge_until_goto=10_000,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1,
        screenshots=True,
    )
    pages = []
    async for page_num, _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=2,
    ):
        pages.append(page_num)

    assert pages == []
    assert len(fake_browser.pages_created[0].screenshot_calls) >= 1


class _FakeProxyPool:
    """Minimal proxy pool: hands out proxies in order and records
    which ones were marked blocked."""

    def __init__(self, proxies: list[dict[str, str]]) -> None:
        self._proxies = list(proxies)
        self._idx = 0
        self.blocked: list[dict[str, str]] = []

    def next(self) -> dict[str, str] | None:
        if self._idx >= len(self._proxies):
            return None
        proxy = self._proxies[self._idx]
        self._idx += 1
        return proxy

    def mark_blocked(self, proxy: dict[str, str]) -> None:
        self.blocked.append(proxy)

    def get_stats(self) -> dict[str, int]:
        return {
            "available": len(self._proxies) - len(self.blocked),
            "total": len(self._proxies),
        }


@pytest.mark.asyncio
async def test_randomized_rotates_proxy_on_antibot(monkeypatch):
    """In per-page (proxy-pool) mode an antibot challenge must mark
    the proxy blocked, cool down and retry the same page through
    the next proxy — and still yield the reviews."""
    cards_by_page = {1: [_make_card("r1"), _make_card("r2")], 2: []}
    # First fetch is fully challenged (both its gotos), the retry
    # fetch gets through — models the per-attempt randomness of
    # Ozon's antibot across proxy exit IPs.
    browsers_created: list[_FakeBrowser] = []
    fetch_count = {"n": 0}

    def fake_import():
        def factory(**kwargs):
            fetch_count["n"] += 1
            b = _FakeBrowser(
                cards_by_page=cards_by_page,
                challenge_until_goto=(
                    100 if fetch_count["n"] == 1 else 0
                ),
            )
            browsers_created.append(b)
            return b
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pool = _FakeProxyPool([
        {"server": "http://proxy-1:10000"},
        {"server": "http://proxy-2:10000"},
    ])
    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0,
        max_idle_pages=1,
        proxy_pool=pool,
    )
    reviews: list[str] = []
    async for _page_num, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=3,
    ):
        reviews.extend(
            r["reviewId"] for r in payload.get("reviews", [])
        )

    assert reviews == ["r1", "r2"]
    # The challenged proxy was marked blocked…
    assert [p["server"] for p in pool.blocked] == [
        "http://proxy-1:10000",
    ]
    # …and the page was retried through a second browser.
    assert len(browsers_created) >= 2


@pytest.mark.asyncio
async def test_warmup_disabled_skips_product_page(monkeypatch):
    """With warmup=False the first navigation goes straight to the
    reviews URL."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1, warmup=False,
    )
    pages = []
    async for page_num, _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pages.append(page_num)

    assert pages == [1]
    urls = fake_browser.pages_created[0].goto_calls
    assert len(urls) == 2  # page 1 + idle page 2, no warmup
    assert all("reviews?page=" in u for u in urls)


@pytest.mark.asyncio
async def test_randomized_rotates_proxy_when_session_fails(monkeypatch):
    """A session-level failure (proxy refuses CONNECT, or the exit
    IP drifts mid-session — invisible-playwright's
    ProxyEgressDrifted) must rotate the proxy and retry the page,
    not crash the run."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    browsers_created: list[_FakeBrowser] = []
    fetch_count = {"n": 0}

    class _DriftedBrowser(_FakeBrowser):
        async def new_page(self):  # type: ignore[override]
            raise RuntimeError(
                "the proxy's egress IP changed during the session"
            )

    def fake_import():
        def factory(**kwargs):
            fetch_count["n"] += 1
            cls = _DriftedBrowser if fetch_count["n"] == 1 else _FakeBrowser
            b = cls(cards_by_page=cards_by_page)
            browsers_created.append(b)
            return b
        return factory

    monkeypatch.setattr(
        pp_module, "_import_invisible_playwright", fake_import,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    pool = _FakeProxyPool([
        {"server": "http://proxy-1:10000"},
        {"server": "http://proxy-2:10000"},
    ])
    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1, proxy_pool=pool,
    )
    reviews: list[str] = []
    async for _, payload in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=3,
    ):
        reviews.extend(r["reviewId"] for r in payload.get("reviews", []))

    # First (drifting) proxy marked blocked, retry through the
    # second one delivered the cards.
    assert reviews == ["r1"]
    assert [p["server"] for p in pool.blocked] == [
        "http://proxy-1:10000",
    ]
    assert len(browsers_created) >= 2


# ---------------------------------------------------------------------------
# Logged-in session cookies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_cookies_injected_into_every_page(monkeypatch):
    """When cookies are configured, every page created by the
    transport gets them via page.context.add_cookies BEFORE any
    navigation (so the first request is already authenticated)."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    session_cookies = [{
        "name": "session_id",
        "value": "abc123",
        "domain": ".ozon.ru",
        "path": "/",
    }]
    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1, cookies=session_cookies,
    )
    pages_seen = []
    async for _ in transport.iter_ozon_reviews_json(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        pass

    for page in fake_browser.pages_created:
        pages_seen.append(page)
        assert page.context.added == session_cookies
        # injection happens before the first navigation
        first_goto_index = 0
        assert page.add_init_script_calls == []  # no stealth script
        assert len(page.goto_calls) > first_goto_index


@pytest.mark.asyncio
async def test_widget_parallel_workers_cover_all_pages(monkeypatch):
    """workers=2: two tabs of one session shard the page space via
    the frontier queue — every page's cards arrive exactly once,
    both tabs participate, and the flow ends when the button
    disappears."""
    cards_by_page = {
        p: [_make_card(f"r{p}")] for p in range(1, 8)  # 7 страниц
    }
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    # A REAL yield (not a plain no-op): the frontier handoff relies
    # on sleep(0) passing control to a worker already blocked on
    # the queue.
    async def _yield_sleep(*a, **kw):
        await _REAL_SLEEP(0)
    monkeypatch.setattr(asyncio, "sleep", _yield_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1, workers=2,
    )
    all_ids: list[str] = []
    async for batch in transport.iter_ozon_reviews_by_widget(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        all_ids.extend(c["uuid"] for c in batch)

    assert sorted(all_ids) == [f"r{p}" for p in range(1, 8)]
    # обе вкладки работали (каждая ходила по своим страницам)
    walked_pages = [
        p for p in fake_browser.pages_created if p.goto_calls
    ]
    assert len(walked_pages) == 2, (
        "одна из вкладок не получила ни одной страницы — "
        "раздача фронтира сломана"
    )


@pytest.mark.asyncio
async def test_widget_parallel_stops_at_max_reviews(monkeypatch):
    """max_reviews stops all workers: the drain loop closes the
    generator, workers get cancelled via the done event."""
    cards_by_page = {
        p: [_make_card(f"r{p}")] for p in range(1, 20)
    }
    _patch_browser(monkeypatch, cards_by_page)

    async def _yield_sleep(*a, **kw):
        await _REAL_SLEEP(0)
    monkeypatch.setattr(asyncio, "sleep", _yield_sleep)

    transport = PublicPageTransport(
        settle_ms=0, lazy_wait_ms=0, max_idle_pages=1, workers=2,
    )
    count = 0
    async for batch in transport.iter_ozon_reviews_by_widget(
        product_path="/product/foo-123",
        max_reviews=5,
        retry_attempts=1,
    ):
        count += len(batch)

    assert count >= 5


@pytest.mark.asyncio
async def test_widget_scroll_mix_collects_lazy_appended_cards(monkeypatch):
    """Scroll-mix: прокрутил страницу — виджет догрузил карточку по
    ходу прокрутки — она подобрана в ту же партию; затем переход на
    следующую страницу (здесь кнопки нет — поток завершается)."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _FakeBrowser(cards_by_page=cards_by_page)

    async def new_page():
        page = _FakePage(
            cards_by_page=cards_by_page, browser=fake_browser,
        )
        fake_browser.pages_created.append(page)

        def on_wheel():
            page.cards_by_page[1] = [
                _make_card("r1"), _make_card("r2_lazy"),
            ]

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

    # lazy_wait_ms по умолчанию: рост определяется по опросам,
    # стабильность из двух одинаковых наборов закрывает ожидание
    transport = PublicPageTransport(settle_ms=0, max_idle_pages=1)
    batches = []
    async for batch in transport.iter_ozon_reviews_by_widget(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        batches.append([c["uuid"] for c in batch])

    assert batches == [["r1", "r2_lazy"]]
    page = fake_browser.pages_created[0]
    # прокрутка действительно выполнялась
    assert page.mouse_wheel_calls, "scroll-mix не прокручивал страницу"


@pytest.mark.asyncio
async def test_widget_scroll_disabled_skips_wheel(monkeypatch):
    """--no-widget-scroll: прокрутки нет, карточки читаются как
    раньше."""
    cards_by_page = {1: [_make_card("r1")], 2: []}
    fake_browser = _patch_browser(monkeypatch, cards_by_page)

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    transport = PublicPageTransport(
        settle_ms=0, max_idle_pages=1, widget_scroll=False,
    )
    batches = []
    async for batch in transport.iter_ozon_reviews_by_widget(
        product_path="/product/foo-123",
        retry_attempts=1,
    ):
        batches.append([c["uuid"] for c in batch])

    assert batches == [["r1"]]
    assert fake_browser.pages_created[0].mouse_wheel_calls == []


# ---------------------------------------------------------------------------
# _wait_for_card_replacement — push-based MutationObserver fast path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_card_replacement_uses_push_observer():
    """When page.evaluate speaks the observer protocol, the wait
    resolves from the single evaluate round-trip — no polling."""

    class _PushPage:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def evaluate(self, expression, arg=None):
            self.calls.append((expression, arg))
            return {"uuids": ["c", "d"]}

    page = _PushPage()
    transport = PublicPageTransport()
    result = await transport._wait_for_card_replacement(
        page, locator=None, prev_uuids={"a", "b"},
    )

    assert result == {"c", "d"}
    assert len(page.calls) == 1
    expression, arg = page.calls[0]
    assert "MutationObserver" in expression
    assert set(arg["prevUuids"]) == {"a", "b"}
    assert arg["timeoutMs"] == transport.card_wait_ms


@pytest.mark.asyncio
async def test_wait_for_card_replacement_observer_timeout():
    """A JS-side timeout resolves to None without falling back to
    the polling loop."""

    class _TimeoutPage:
        async def evaluate(self, expression, arg=None):
            return {"timeout": True}

    transport = PublicPageTransport()
    result = await transport._wait_for_card_replacement(
        _TimeoutPage(), locator=None, prev_uuids={"a"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_wait_for_card_replacement_unknown_shape_falls_back():
    """A page that returns an unknown evaluate shape (unit-test
    fakes, exotic engines) falls back to the legacy poll loop."""

    class _LegacyPage:
        async def evaluate(self, expression, arg=None):
            # Unknown shape: a bare list, not the observer dict.
            return ["x"]

    # card_wait_ms=0 → the fallback poll loop's deadline has
    # already passed → returns None immediately.
    transport = PublicPageTransport(card_wait_ms=0)
    result = await transport._wait_for_card_replacement(
        _LegacyPage(), locator=None, prev_uuids={"x"},
    )
    assert result is None
