"""Tests for the Yandex.Market browser transport.

Uses a fake page/browser (the same pattern as
test_public_page_transport.py) — no network, no real browser.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import infrastructure.transports.yandex_browser as yb
from infrastructure.transports.yandex_browser import (
    YANDEX_MARKET_BASE,
    YandexBrowserTransport,
    YandexCaptchaError,
    _parse_float,
    _parse_int,
)

PRODUCT_URL = (
    "https://market.yandex.ru/card/smartfon-x/12345678"
)

# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeContext:
    def __init__(self) -> None:
        self.cookies_added: list[dict[str, Any]] = []
        self.saved_cookies: list[dict[str, Any]] = [
            {"name": "session", "value": "1"},
        ]
        #: Open tabs as _close_stray_tabs sees them (real Playwright
        #: exposes context.pages; empty = no strays for most tests).
        self.pages: list[Any] = []

    async def add_cookies(
        self, cookies: list[dict[str, Any]],
    ) -> None:
        self.cookies_added.extend(cookies)

    async def cookies(self) -> list[dict[str, Any]]:
        return list(self.saved_cookies)


class _FakeMouse:
    def __init__(self) -> None:
        self.wheel_calls: list[int] = []
        self.move_calls: list[tuple[int, int]] = []

    async def move(self, x: int, y: int) -> None:
        self.move_calls.append((x, y))

    async def wheel(self, x: int, y: int) -> None:
        self.wheel_calls.append(y)


class _FakeRoute:
    def __init__(self, resource_type: str) -> None:
        self.request = type(
            "Request", (), {"resource_type": resource_type},
        )()
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeLocator:
    def __init__(self, present: bool = False) -> None:
        self.present = present
        self.clicked = 0

    async def count(self) -> int:
        return 1 if self.present else 0

    @property
    def first(self) -> _FakeLocator:
        return self

    async def click(self) -> None:
        self.clicked += 1


class _FakePage:
    """Plays the paginated reviews pages: fixed card lists per page,
    optional captcha state, optional per-page JSON-LD reviews."""

    def __init__(
        self,
        *,
        cards_by_page: dict[int, list[dict[str, Any]]]
        | None = None,
        ld_by_page: dict[int, list[dict[str, Any]]]
        | None = None,
        total_count: str = "1 234",
        average_rating: str = "4.7",
        captcha_url: bool = False,
        captcha_html: bool = False,
    ) -> None:
        self.cards_by_page = cards_by_page or {}
        self.ld_by_page = ld_by_page or {}
        self.total_count = total_count
        self.average_rating = average_rating
        self.captcha_url = captcha_url
        self.captcha_html = captcha_html
        self.goto_calls: list[dict[str, Any]] = []
        self.current_page = 0  # 0 = the warmup (card) page
        self.routes: dict[str, Any] = {}
        self.content_calls = 0
        self._mouse = _FakeMouse()
        self._context = _FakeContext()
        self.show_more = _FakeLocator(present=False)
        # Optional test hook: callable(expression) -> payload.
        self.extra_evaluate: Any = None

    @property
    def url(self) -> str:
        if self.captcha_url:
            return "https://market.yandex.ru/showcaptcha?xyz"
        return (
            f"{YANDEX_MARKET_BASE}/card/smartfon-x/12345678"
            f"/reviews?page={max(self.current_page, 1)}"
        )

    @property
    def viewport_size(self) -> dict[str, int]:
        return {"width": 1280, "height": 720}

    @property
    def frames(self) -> list[Any]:
        return []

    async def add_init_script(self, script: str) -> None:
        return None

    @property
    def mouse(self) -> _FakeMouse:
        return self._mouse

    @property
    def context(self) -> _FakeContext:
        return self._context

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})
        if "page=" in url:
            try:
                self.current_page = int(
                    url.split("page=")[1].split("&")[0]
                )
            except (ValueError, IndexError):
                self.current_page = 1
        else:
            self.current_page = 0

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def content(self) -> str:
        self.content_calls += 1
        if self.captcha_html:
            return (
                "<html>Подтвердите, что запросы "
                "отправляли вы</html>"
            )
        return f"<html>page {self.current_page}</html>"

    async def evaluate(
        self, expression: str, *args: Any
    ) -> Any:
        if expression == yb._READ_CARDS_JS:
            if self.extra_evaluate is not None:
                return self.extra_evaluate(expression)
            # A captcha shell carries NO SSR data whatsoever —
            # classify() keys on exactly that.
            if self.captcha_url or self.captcha_html:
                return {
                    "cards": [],
                    "ld_reviews": [],
                    "total_count": None,
                    "average_rating": None,
                }
            return {
                "cards": self.cards_by_page.get(
                    self.current_page, []
                ),
                "ld_reviews": self.ld_by_page.get(
                    self.current_page, []
                ),
                "total_count": self.total_count,
                "average_rating": self.average_rating,
            }
        return None

    def locator(self, selector: str) -> _FakeLocator:
        if (
            "showMore" in selector
            or "show-more" in selector
            or "Показать ещё" in selector
        ):
            return self.show_more
        return _FakeLocator(present=False)

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes[pattern] = handler

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.pages_created: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        self.pages_created.append(self.page)
        return self.page

    async def __aenter__(self) -> _FakeBrowser:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    page: _FakePage,
) -> _FakeBrowser:
    browser = _FakeBrowser(page)

    def fake_import() -> Any:
        def factory(**kwargs: Any) -> _FakeBrowser:
            return browser

        return factory

    monkeypatch.setattr(
        yb, "_import_invisible_playwright", fake_import,
    )
    return browser


async def _noop_sleep(seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)


def _make_transport(**kwargs: Any) -> YandexBrowserTransport:
    defaults: dict[str, Any] = {
        "settle_ms": 0,
        "scroll_max_idle_rounds": 2,
        "cookies_path": None,
        "page_delay_seconds": 0.0,
        # The captcha ladder's waits use REAL loop time (the fake
        # wait_for_timeout returns instantly), so shrink them to
        # zero / disable the manual pause for tests.
        "auto_captcha_wait_s": 0.0,
        "manual_captcha": False,
    }
    defaults.update(kwargs)
    return YandexBrowserTransport(**defaults)


def _card(
    uuid: str,
    *,
    rating: Any = None,
    author: str = "Иван",
    date: str = "28 января 2024",
    text: str = "Текст",
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "rating": rating,
        "text": text,
        "author": author,
        "date": date,
    }


# ----------------------------------------------------------------------
# Warmup / navigation / pagination
# ----------------------------------------------------------------------


async def test_warmup_navigates_to_card_first(monkeypatch):
    page = _FakePage(cards_by_page={1: [_card("y1")]})
    _patch(monkeypatch, page)

    transport = _make_transport()
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert page.goto_calls[0]["url"] == (
        f"{YANDEX_MARKET_BASE}/card/smartfon-x/12345678"
    )
    # Reviews navigation carries the card page as the referer and
    # starts from ?page=1.
    assert page.goto_calls[1]["url"].endswith(
        "/reviews?page=1"
    )
    assert page.goto_calls[1]["referer"].endswith("/12345678")


async def test_single_page_stops_on_empty_next_page(monkeypatch):
    page = _FakePage(
        cards_by_page={1: [_card("y1"), _card("y2")], 2: []},
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1", "y2"]]
    # Walked page 1 (plus lazy re-read round) then probed page 2.
    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    assert visited[-1].endswith("page=2")


async def test_multi_page_pagination(monkeypatch):
    page = _FakePage(
        cards_by_page={
            1: [_card("y1"), _card("y2")],
            2: [_card("y3")],
            3: [],
        },
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1", "y2"], ["y3"]]
    # Page 3 was probed and found empty — run stopped.
    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    assert visited[-1].endswith("page=3")


async def test_lazy_appended_cards_merged_into_page_batch(
    monkeypatch,
):
    page = _FakePage(cards_by_page={1: []})
    # The AB variant that paginates with a "Show more" button.
    page.show_more = _FakeLocator(present=True)
    _patch(monkeypatch, page)
    state = {"round": 0}

    def fake_evaluate(expression: str) -> Any:
        state["round"] += 1
        cards = [_card("y1")]
        if state["round"] >= 2:
            cards.append(_card("y2"))
        return {
            "cards": cards,
            "ld_reviews": [],
            "total_count": "10",
            "average_rating": "4.5",
        }

    page.extra_evaluate = fake_evaluate

    transport = _make_transport(scroll_max_idle_rounds=3)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    # Round 1 saw y1, the Show-More click woke y2, round 2 picked
    # it up — both land in ONE page batch; the next page is empty.
    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1", "y2"]]


async def test_duplicate_page_revisited_stops(monkeypatch):
    """A site that serves the same cards for every ?page=N must not
    loop forever: the dedup turns them into empty pages."""
    page = _FakePage(
        cards_by_page={
            1: [_card("y1")],
            2: [_card("y1")],  # same card again
        },
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"]]


async def test_fetch_total_count_probes_without_walking(monkeypatch):
    """The counter probe reads page 1's totals and STOPS — no walk
    past page 1, no batches yielded."""
    page = _FakePage(
        cards_by_page={
            1: [_card("y1")],
            2: [_card("y2")],  # must never be reached
        },
        total_count="1 234",
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    total = await transport.fetch_total_count(PRODUCT_URL)

    assert total == 1234
    assert transport.last_total_count == 1234
    # Warmup (card page) + reviews page 1 — nothing further.
    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    assert visited[-1].endswith("page=1")


async def test_fetch_total_count_none_without_counter(monkeypatch):
    """A healthy page without a JSON-LD counter → None (the caller
    downgrades to a single session instead of guessing)."""
    page = _FakePage(
        cards_by_page={1: [_card("y1")]},
        total_count=None,
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    assert await transport.fetch_total_count(PRODUCT_URL) is None


async def test_duplicate_streak_stops_walk(monkeypatch):
    """Past the last page Yandex re-serves old ground instead of an
    empty page: after dup_pages_stop consecutive pages with ZERO
    new cards the walk must stop, not grind on forever. No site
    counter here — the base (unknown-total) behaviour."""
    # Every page serves the same card — no empty page ever comes.
    cards = [_card("y1")]
    page = _FakePage(
        cards_by_page={i: cards for i in range(1, 20)},
        total_count=None,
    )
    _patch(monkeypatch, page)

    transport = _make_transport(dup_pages_stop=2)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"]]
    # Page 1 was productive; pages 2-3 were the dup streak that
    # ended the walk (NOT all 19 pages).
    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    assert len(visited) == 3
    assert visited[-1].endswith("page=3")


async def test_dup_streak_extended_while_counter_remaining(
    monkeypatch,
):
    """The 2026-09-19 incident: the expansion PRELOADED the next
    windows, three pages came back all-dup while the site counter
    said reviews remain — the walk must go THROUGH the preloaded
    ground and pick the fresh cards beyond instead of stopping."""
    page = _FakePage(
        cards_by_page={
            1: [_card(f"y{i}") for i in range(10)],
            # Pages 2-4 re-serve page 1's window (preloaded).
            **{
                i: [_card(f"y{j}") for j in range(10)]
                for i in (2, 3, 4)
            },
            # Page 5 finally brings the next window.
            5: [_card(f"y{i}") for i in range(10, 20)],
            # Past page 5 the pager wraps (everything seen).
            **{
                i: [_card(f"y{j}") for j in range(20)]
                for i in range(6, 30)
            },
        },
        total_count="20",
    )
    _patch(monkeypatch, page)

    transport = _make_transport(dup_pages_stop=3)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    ids = [c["uuid"] for b in batches for c in b]
    assert sorted(ids) == sorted(
        f"y{i}" for i in range(20)
    )
    # Walked through the preloaded streak (2-4), picked page 5's
    # fresh window, then stopped on the wrap (6-8).
    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    assert visited[-1].endswith("page=8")


async def test_dup_streak_extension_is_capped(monkeypatch):
    """An unattainable counter (textless «оценки», wrapped pager)
    must not grind forever: the extension is capped at
    _DUP_PAGES_MAX_EXTENSION pages."""
    cards = [_card("y1")]
    page = _FakePage(
        cards_by_page={i: cards for i in range(1, 60)},
        # 999 "remaining" — far beyond the cap.
        total_count="1000",
    )
    _patch(monkeypatch, page)

    transport = _make_transport(dup_pages_stop=3)
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    # Page 1 productive + (3 base + 25 capped) dup pages = 29.
    assert len(visited) == 29


async def test_duplicate_streak_zero_disables_stop(monkeypatch):
    """dup_pages_stop=0 restores the old walk-to-empty-page
    behaviour."""
    page = _FakePage(
        cards_by_page={
            1: [_card("y1")],
            2: [_card("y1")],  # dup page must NOT stop the walk
            3: [],
        },
    )
    _patch(monkeypatch, page)

    transport = _make_transport(dup_pages_stop=0)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"]]
    visited = [
        call["url"] for call in page.goto_calls
        if "page=" in call["url"]
    ]
    assert visited[-1].endswith("page=3")  # the empty page


async def test_one_dup_page_does_not_stop_walk(monkeypatch):
    """A single all-dup page is normal (Show-More preloads the next
    page's SSR cards) — the walk must continue and pick up the new
    cards two pages later."""
    page = _FakePage(
        cards_by_page={
            1: [_card("y1")],
            2: [_card("y1")],  # preloaded by Show-More on page 1
            3: [_card("y2")],
            4: [],
        },
    )
    _patch(monkeypatch, page)

    transport = _make_transport(dup_pages_stop=2)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"], ["y2"]]


# ----------------------------------------------------------------------
# JSON-LD: rating merge, fallback, totals
# ----------------------------------------------------------------------


async def test_rating_merged_from_json_ld(monkeypatch):
    page = _FakePage(
        cards_by_page={
            1: [
                _card("y1", author="Иван"),
                _card("y2", author="Мария"),
            ],
        },
        ld_by_page={
            1: [
                {
                    "author": "Иван",
                    "date": "2024-01-28",
                    "text": "Текст",
                    "rating": 5,
                },
            ],
        },
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    ratings = {
        c["uuid"]: c["rating"] for c in batches[0]
    }
    assert ratings["y1"] == 5
    assert ratings["y2"] is None


async def test_json_ld_fallback_when_dom_empty(monkeypatch):
    ld = {
        "author": "Иван",
        "date": "2024-01-28",
        "text": "Хороший товар",
        "rating": 4,
    }
    page = _FakePage(
        cards_by_page={1: []},
        ld_by_page={1: [ld]},
        total_count="14",
        average_rating="4.8",
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert len(batches) == 1
    card = batches[0][0]
    assert card["author"] == "Иван"
    assert card["rating"] == 4
    assert card["text"] == "Хороший товар"
    assert transport.last_total_count == 14
    assert transport.last_average_rating == 4.8


async def test_totals_parsed_from_strings(monkeypatch):
    page = _FakePage(
        cards_by_page={1: [_card("y1")]},
        total_count="1 234",
        average_rating="4.7",
    )
    _patch(monkeypatch, page)

    transport = _make_transport()
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert transport.last_total_count == 1234
    assert transport.last_average_rating == 4.7


# ----------------------------------------------------------------------
# Cookies, routes, show-more
# ----------------------------------------------------------------------


async def test_cookies_injected_and_saved(monkeypatch, tmp_path):
    page = _FakePage(cards_by_page={1: [_card("y1")]})
    _patch(monkeypatch, page)
    cookies_path = tmp_path / "yandex_cookies.json"

    transport = _make_transport(
        cookies=[{"name": "c", "value": "v"}],
        cookies_path=str(cookies_path),
    )
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert page._context.cookies_added == [
        {"name": "c", "value": "v"}
    ]
    saved = json.loads(
        cookies_path.read_text(encoding="utf-8"),
    )
    assert saved == [{"name": "session", "value": "1"}]


async def test_resource_blocker_installed(monkeypatch):
    page = _FakePage(cards_by_page={1: [_card("y1")]})
    _patch(monkeypatch, page)

    # OFF by default (a real browser loads images/fonts — see the
    # transport docstring); opt back in explicitly.
    transport = _make_transport(block_assets=True)
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert any("*." in p for p in page.routes)

    route = _FakeRoute("image")
    await page.routes["**/*.png"](route)
    assert route.aborted

    route = _FakeRoute("document")
    await page.routes["**/*.png"](route)
    assert route.continued


async def test_resource_blocker_off_by_default(monkeypatch):
    page = _FakePage(cards_by_page={1: [_card("y1")]})
    _patch(monkeypatch, page)

    transport = _make_transport()
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert not page.routes


async def test_show_more_clicked_when_present(monkeypatch):
    page = _FakePage(cards_by_page={1: [_card("y1")]})
    page.show_more = _FakeLocator(present=True)
    _patch(monkeypatch, page)

    transport = _make_transport()
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert page.show_more.clicked >= 1


# ----------------------------------------------------------------------
# Captcha
# ----------------------------------------------------------------------


async def test_captcha_retries_then_succeeds(monkeypatch):
    page = _FakePage(
        cards_by_page={1: [_card("y1")]},
        captcha_html=True,
    )
    _patch(monkeypatch, page)

    # Captcha clears after the first challenged navigation.
    original_content = page.content

    async def content_once() -> str:
        result = await original_content()
        page.captcha_html = False
        return result

    page.content = content_once  # type: ignore[method-assign]

    transport = _make_transport(captcha_max_attempts=3)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert transport.captcha_hits == 1
    assert [c["uuid"] for c in batches[0]] == ["y1"]


async def test_captcha_url_marker_detected(monkeypatch):
    page = _FakePage(cards_by_page={}, captcha_url=True)
    _patch(monkeypatch, page)

    transport = _make_transport(captcha_max_attempts=2)
    with pytest.raises(YandexCaptchaError):
        async for _ in transport.iter_review_batches(
            PRODUCT_URL,
        ):
            pass

    assert transport.captcha_hits == 2


async def test_captcha_gives_up_after_max_attempts(monkeypatch):
    page = _FakePage(cards_by_page={}, captcha_html=True)
    _patch(monkeypatch, page)

    transport = _make_transport(captcha_max_attempts=2)
    with pytest.raises(YandexCaptchaError):
        async for _ in transport.iter_review_batches(
            PRODUCT_URL,
        ):
            pass

    assert transport.captcha_hits == 2


async def test_captcha_page_dumped_to_debug_dir(
    monkeypatch, tmp_path,
):
    page = _FakePage(cards_by_page={}, captcha_url=True)
    _patch(monkeypatch, page)

    debug_dir = tmp_path / "debug_yandex"
    transport = _make_transport(
        debug_dir=str(debug_dir),
        captcha_max_attempts=1,
    )
    with pytest.raises(YandexCaptchaError):
        async for _ in transport.iter_review_batches(
            PRODUCT_URL,
        ):
            pass

    assert list(debug_dir.glob("captcha_*.html"))


async def test_reviews_page_dumped_to_debug_dir(
    monkeypatch, tmp_path,
):
    page = _FakePage(cards_by_page={1: [_card("y1")]})
    _patch(monkeypatch, page)

    debug_dir = tmp_path / "debug_yandex"
    transport = _make_transport(debug_dir=str(debug_dir))
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert (debug_dir / "reviews_page.html").exists()


async def test_healthy_pages_skip_page_content(
    monkeypatch, tmp_path,
):
    """The hot path must not serialize the multi-megabyte markup
    per read: page.content() runs only for the debug dumps, never
    for card reads of healthy pages."""
    page = _FakePage(
        cards_by_page={
            1: [_card("y1"), _card("y2")],
            2: [_card("y3")],
            3: [],
        },
    )
    _patch(monkeypatch, page)

    debug_dir = tmp_path / "debug_yandex"
    transport = _make_transport(debug_dir=str(debug_dir))
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    # Three healthy card reads happened, but content() was called
    # only twice: the first-reviews-page dump + the empty-page
    # dump. (Without the lazy read it would be 3+.)
    assert page.content_calls == 2


async def test_empty_page_dumped_to_debug_dir(
    monkeypatch, tmp_path,
):
    page = _FakePage(
        cards_by_page={1: [_card("y1")], 2: []},
    )
    _patch(monkeypatch, page)

    debug_dir = tmp_path / "debug_yandex"
    transport = _make_transport(debug_dir=str(debug_dir))
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert (debug_dir / "empty_page_2.html").exists()


def test_classify_markers():
    classify = YandexBrowserTransport()._classify

    # Redirect captcha — URL markers win regardless of body.
    assert classify(
        "https://market.yandex.ru/showcaptcha?x", {},
    ) == yb._PAGE_CAPTCHA

    # Inline SmartCaptcha shells — body markers on a small body.
    assert classify(
        "",
        {"html": "<html>SmartCaptcha</html>", "body_len": 16_000},
    ) == yb._PAGE_CAPTCHA
    assert classify(
        "",
        {
            "html": "<html>Вы не робот?</html>",
            "body_len": 16_000,
        },
    ) == yb._PAGE_CAPTCHA
    assert classify(
        "",
        {
            "html": '<form action="/checkcaptcha?key=x">',
            "body_len": 16_000,
        },
    ) == yb._PAGE_CAPTCHA

    # Healthy page: SSR data present.
    assert classify(
        "",
        {"cards": [{"uuid": "y1"}], "body_len": 1_400_000},
    ) == yb._PAGE_HEALTHY
    assert classify(
        "",
        {"total_count": "14", "body_len": 1_400_000},
    ) == yb._PAGE_HEALTHY

    # A healthy megabyte page that merely MENTIONS captcha-ish
    # strings in its own scripts must NOT be flagged (the Ozon
    # README's "antibot" lesson) — SSR data present, big body.
    assert classify(
        "https://market.yandex.ru/card/x/1/reviews",
        {
            "html": "<html>обычная страница, SmartCaptcha упоминается</html>",
            "cards": [{"uuid": "y1"}],
            "body_len": 1_400_000,
        },
    ) == yb._PAGE_HEALTHY

    # Markerless degraded shell: no SSR data, no captcha markers.
    assert classify(
        "https://market.yandex.ru/card/x/1/reviews",
        {"html": "<html>пусто</html>", "body_len": 15_000},
    ) == yb._PAGE_DEGRADED

    assert classify("", {}) == yb._PAGE_DEGRADED


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1234, 1234),
        ("1234", 1234),
        ("1 234", 1234),
        ("1 234 отзыва", 1234),
        ("1234 отзыва", 1234),
        (None, None),
        ("", None),
        ("abc", None),
        (True, None),
    ],
)
def test_parse_int(value, expected):
    assert _parse_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (4.7, 4.7),
        ("4.7", 4.7),
        ("4,7", 4.7),
        ("4.7 из 5", 4.7),
        (None, None),
        ("", None),
        (True, None),
    ],
)
def test_parse_float(value, expected):
    assert _parse_float(value) == expected


def test_card_key_fallback():
    card = {
        "author": "Иван",
        "date": "28 января 2024",
        "rating": 5,
        "text": "Отличный товар",
    }
    key = YandexBrowserTransport._card_key(card)
    assert "Иван" in key and "Отличный товар" in key

    assert (
        YandexBrowserTransport._card_key({"uuid": "abc"})
        == "abc"
    )


# ----------------------------------------------------------------------
# Scroll-based lazy expansion + dedup
# ----------------------------------------------------------------------


async def test_scroll_wakes_lazy_cards_and_dedups(monkeypatch):
    """No Show-More button: realistic wheel strides wake the lazy
    cards, and every round's re-read returns the WHOLE loaded list
    — the per-card dedup must yield each review exactly once."""
    page = _FakePage(cards_by_page={1: [], 2: []})
    _patch(monkeypatch, page)

    # The lazy DOM: the number of loaded cards tracks the number of
    # wheel strides performed (an intersection observer would fire
    # the same way); each read returns everything loaded so far.
    def fake_evaluate(expression: str) -> Any:
        if page.current_page != 1:
            return {
                "cards": [],
                "ld_reviews": [],
                "total_count": "3",
                "average_rating": "4.5",
                "scroll_bottom_gap": 0,
            }
        loaded = min(3, len(page._mouse.wheel_calls))
        return {
            "cards": [_card(f"y{i}") for i in range(loaded)],
            "ld_reviews": [],
            "total_count": "3",
            "average_rating": "4.5",
            # Still mid-document: the loop must NOT take the
            # at-the-bottom early exit.
            "scroll_bottom_gap": 5_000,
        }

    page.extra_evaluate = fake_evaluate

    transport = _make_transport(scroll_max_idle_rounds=2)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    # All three lazy cards arrived via the scroll rounds…
    ids = [
        c["uuid"] for batch in batches for c in batch
    ]
    assert ids == ["y0", "y1", "y2"]
    # …the wheel actually scrolled, and each uuid appears exactly
    # once despite every re-read returning the accumulated list.
    assert page._mouse.wheel_calls
    assert len(ids) == len(set(ids))


async def test_scroll_stops_early_at_document_bottom(monkeypatch):
    """A page with no lazy loading: after ONE unproductive scroll
    round at the document bottom the expansion stops — no idle
    budget burned, no time wasted."""
    page = _FakePage(cards_by_page={1: [_card("y1")], 2: []})
    _patch(monkeypatch, page)
    reads = {"n": 0}

    def fake_evaluate(expression: str) -> Any:
        reads["n"] += 1
        return {
            # Page 1 has the SSR card and nothing ever lazy-loads;
            # page 2 is the walk-ending empty page.
            "cards": (
                [_card("y1")] if page.current_page == 1 else []
            ),
            "ld_reviews": [],
            "total_count": "1",
            "average_rating": "5",
            # Already sitting at the bottom of a short page.
            "scroll_bottom_gap": 0,
        }

    page.extra_evaluate = fake_evaluate

    transport = _make_transport(scroll_max_idle_rounds=5)
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"]]
    # Initial read (goto) + ONE expansion round read + page 2's
    # empty read — the idle budget of 5 was never burned.
    assert reads["n"] == 3


# ----------------------------------------------------------------------
# Stray tabs
# ----------------------------------------------------------------------


class _StrayTab:
    """A tab popped by an ad / target=_blank wrapper."""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_show_more_stray_tabs_closed(monkeypatch):
    """A Show-More click that pops extra tabs: the strays are
    closed, the walk page survives."""
    page = _FakePage(cards_by_page={1: [_card("y1")], 2: []})
    page.show_more = _FakeLocator(present=True)
    stray_ad = _StrayTab()
    stray_blank = _StrayTab()
    # The context reports the walk page + two strays (what a real
    # context.pages would show right after the click).
    page._context.pages = [page, stray_ad, stray_blank]
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"]]
    assert stray_ad.closed >= 1
    assert stray_blank.closed >= 1
    # The walk page was NOT closed (the batch above proves it, but
    # assert the identity check directly).
    assert all(tab is not page for tab in (stray_ad, stray_blank))


async def test_no_stray_tabs_no_op(monkeypatch):
    """Without strays (empty context.pages) the walk is unaffected."""
    page = _FakePage(cards_by_page={1: [_card("y1")], 2: []})
    page.show_more = _FakeLocator(present=True)
    _patch(monkeypatch, page)

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert [
        [c["uuid"] for c in batch] for batch in batches
    ] == [["y1"]]


# ----------------------------------------------------------------------
# Own vs cross-product feed (oskuId rule)
# ----------------------------------------------------------------------


def test_filter_own_cards_by_offer_id():
    """The offer-info oskuId is authoritative: foreign ids are
    dropped, the open product's id is accepted, chip-less cards
    fall through to the legacy heuristics."""
    cards = [
        # Own chip → hard accept.
        {
            "uuid": "own-1",
            "offer_id": "12345678",
            "author": "Иван",
            "date": "28 января 2024",
            "text": "Свой отзыв",
        },
        # Foreign chip → hard drop even with full content.
        {
            "uuid": "feed-1",
            "offer_id": "999999999",
            "author": "Мария",
            "date": "28 января 2024",
            "text": "Чужой отзыв из ленты",
        },
        # No chip → legacy heuristics (uuid branch keeps it).
        {
            "uuid": "nochip-1",
            "author": "Пётр",
            "date": "29 января 2024",
            "text": "Отзыв без чипа",
        },
    ]
    own = YandexBrowserTransport._filter_own_cards(
        cards, [], "12345678",
    )
    assert [c["uuid"] for c in own] == ["own-1", "nochip-1"]

    # Without a product id the oskuId rule is inert (probe /
    # layout drift) — everything falls to the heuristics.
    own_all = YandexBrowserTransport._filter_own_cards(
        cards, [], None,
    )
    assert [c["uuid"] for c in own_all] == [
        "own-1", "feed-1", "nochip-1",
    ]


async def test_walk_drops_foreign_feed_cards(monkeypatch):
    """End-to-end through _read_cards: the warmup-derived product
    id (12345678 from PRODUCT_URL) filters the feed cards out of
    the yielded batches."""
    page = _FakePage(cards_by_page={1: [], 2: []})
    _patch(monkeypatch, page)

    def fake_evaluate(expression: str) -> Any:
        return {
            "cards": [
                {
                    "uuid": "own-1",
                    "offer_id": "12345678",
                    "author": "Иван",
                    "date": "28 января 2024",
                    "text": "Свой",
                },
                {
                    "uuid": "feed-1",
                    "offer_id": "42",
                    "author": "Мария",
                    "date": "28 января 2024",
                    "text": "Чужой",
                },
            ],
            "ld_reviews": [],
            "total_count": "250",
            "average_rating": "4.6",
            "scroll_bottom_gap": 0,
        }

    page.extra_evaluate = fake_evaluate

    transport = _make_transport()
    batches = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    ids = [c["uuid"] for b in batches for c in b]
    assert ids == ["own-1"]
    assert transport._current_product_id == "12345678"


async def test_product_name_flows_to_totals(monkeypatch):
    """The JSON-LD Product name must survive _read_cards into
    last_product_name (the unified output's product_title) — it
    used to be dropped from the snapshot dict."""
    page = _FakePage(cards_by_page={1: []})
    _patch(monkeypatch, page)

    def fake_evaluate(expression: str) -> Any:
        return {
            "cards": (
                [_card("y1")] if page.current_page == 1 else []
            ),
            "ld_reviews": [],
            "total_count": "10",
            "average_rating": "4.5",
            "product_name": "Экдистерон 400мг, 30 капсул",
            "scroll_bottom_gap": 0,
        }

    page.extra_evaluate = fake_evaluate

    transport = _make_transport()
    _ = [
        batch async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        )
    ]

    assert (
        transport.last_product_name
        == "Экдистерон 400мг, 30 капсул"
    )
