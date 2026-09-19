"""Tests for the Yandex.Market parallel page-range sessions:

- the transport's ``start_page`` / ``max_pages`` bounds (with the
  held-back batch flushed at range end), driven against a fake
  paginated page;
- ``_iter_parallel_reviews`` partial-failure semantics.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import infrastructure.transports.yandex_browser as yb
from infrastructure.transports.yandex_browser import (
    YANDEX_MARKET_BASE,
    YandexBrowserTransport,
    YandexCaptchaError,
)
from marketplace_maps_parser.collectors import (
    _iter_parallel_reviews,
)

PRODUCT_URL = "https://market.yandex.ru/card/smartfon-x/12345678"


# ----------------------------------------------------------------------
# Fakes (the same shape as test_yandex_browser_transport.py, trimmed
# to what the page-range walk touches)
# ----------------------------------------------------------------------


class _FakeContext:
    async def add_cookies(
        self, cookies: list[dict[str, Any]],
    ) -> None:
        return None

    async def cookies(self) -> list[dict[str, Any]]:
        return []


class _FakeMouse:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def move(self, x: int, y: int) -> None:
        return None

    async def wheel(self, x: int, y: int) -> None:
        self._page.wheel_events.append(
            (self._page.current_page, y),
        )


class _FakeLocator:
    async def count(self) -> int:
        return 0

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def click(self) -> None:
        return None


class _FakeLink:
    def __init__(self, href: str, page: "_FakePage") -> None:
        self._href = href
        self._page = page

    async def get_attribute(self, name: str) -> str:
        return self._href

    async def is_visible(self) -> bool:
        return True

    async def click(self, timeout: int = 0) -> None:
        if "page=" in self._href:
            try:
                page_no = int(
                    self._href.split("page=")[1].split("&")[0],
                )
            except (ValueError, IndexError):
                page_no = 1
        elif "/reviews" in self._href:
            page_no = 1
        else:
            return
        self._page.current_page = page_no
        self._page.goto_pages.append(page_no)


class _FakeLinks:
    def __init__(
        self, hrefs: list[str], page: "_FakePage",
    ) -> None:
        self._hrefs = hrefs
        self._page = page

    async def count(self) -> int:
        return len(self._hrefs)

    def nth(self, i: int) -> _FakeLink:
        return _FakeLink(self._hrefs[i], self._page)


class _FakePage:
    """Plays healthy paginated reviews pages: fixed cards per page
    (every page has SSR data → `_classify` sees it healthy).
    ``pager_links`` / ``reviews_links`` simulate the site's own
    navigation links for the click-first path."""

    def __init__(
        self, cards_by_page: dict[int, list[dict[str, Any]]],
        *,
        pager_links: list[str] | None = None,
        reviews_links: list[str] | None = None,
    ) -> None:
        self.cards_by_page = cards_by_page
        self.pager_links = pager_links
        self.reviews_links = reviews_links
        self.current_page = 0  # 0 = the warmup (card) page
        self.goto_pages: list[int] = []
        self.goto_calls: list[dict[str, Any]] = []
        self.wheel_events: list[tuple[int, int]] = []

    @property
    def url(self) -> str:
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

    @property
    def mouse(self) -> _FakeMouse:
        return _FakeMouse(self)

    @property
    def context(self) -> _FakeContext:
        return _FakeContext()

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})
        if "page=" in url:
            try:
                self.current_page = int(
                    url.split("page=")[1].split("&")[0]
                )
            except (ValueError, IndexError):
                self.current_page = 1
            self.goto_pages.append(self.current_page)
        else:
            self.current_page = 0

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def content(self) -> str:
        return f"<html>page {self.current_page}</html>"

    async def evaluate(
        self, expression: str, *args: Any
    ) -> Any:
        if expression == yb._READ_CARDS_JS:
            return {
                "cards": self.cards_by_page.get(
                    self.current_page, [],
                ),
                "ld_reviews": [],
                "total_count": "40",
                "average_rating": "4.7",
            }
        return None

    def locator(self, selector: str) -> Any:
        if (
            "page=" in selector
            and self.pager_links is not None
        ):
            return _FakeLinks(self.pager_links, self)
        if (
            "/reviews" in selector
            and self.reviews_links is not None
        ):
            return _FakeLinks(self.reviews_links, self)
        return _FakeLocator()

    async def add_init_script(self, script: str) -> None:
        return None

    async def route(self, pattern: str, handler: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def add_init_script(self, script: str) -> None:
        return None

    async def route(self, pattern: str, handler: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page

    async def __aenter__(self) -> "_FakeBrowser":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _patch_import(
    monkeypatch: pytest.MonkeyPatch, page: _FakePage,
) -> None:
    browser = _FakeBrowser(page)

    def fake_import() -> Any:
        def factory(**kwargs: Any) -> _FakeBrowser:
            return browser

        return factory

    monkeypatch.setattr(
        yb, "_import_invisible_playwright", fake_import,
    )


def _transport(**kwargs: Any) -> YandexBrowserTransport:
    defaults: dict[str, Any] = {
        "settle_ms": 0,
        "scroll_max_idle_rounds": 2,
        "cookies_path": None,
        "page_delay_seconds": 0.0,
        "debug_dir": "debug_test_yandex_parallel",
    }
    defaults.update(kwargs)
    return YandexBrowserTransport(**defaults)


def _card(page: int, n: int) -> dict[str, Any]:
    return {
        "uuid": f"p{page}i{n}",
        "author": f"автор {page}-{n}",
        "date": f"1 января 202{page}",
        "text": f"текст {page}-{n}",
        "rating": 5,
    }


def _collect(transport: YandexBrowserTransport) -> list[str]:
    async def run() -> list[str]:
        ids: list[str] = []
        async for batch in transport.iter_review_batches(
            PRODUCT_URL,
        ):
            ids.extend(card["uuid"] for card in batch)
        return ids

    return asyncio.run(run())


_THREE_PAGES = {
    page: [_card(page, i) for i in range(2)]
    for page in (1, 2, 3)
}


def test_max_pages_bounds_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(_THREE_PAGES)
    _patch_import(monkeypatch, page)
    transport = _transport(max_pages=2)
    ids = _collect(transport)
    # Page 3 is never navigated; pages 1-2 collected in full (the
    # final held-back batch flushed at range end).
    assert sorted(ids) == ["p1i0", "p1i1", "p2i0", "p2i1"]
    assert page.goto_pages == [1, 2]


def test_start_page_skips_earlier_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(_THREE_PAGES)
    _patch_import(monkeypatch, page)
    transport = _transport(start_page=2, max_pages=2)
    ids = _collect(transport)
    assert sorted(ids) == ["p2i0", "p2i1", "p3i0", "p3i1"]
    assert page.goto_pages == [2, 3]


def test_empty_page_still_ends_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage({1: _THREE_PAGES[1]})  # page 2 is empty
    _patch_import(monkeypatch, page)
    transport = _transport(max_pages=5)
    ids = _collect(transport)
    assert ids == ["p1i0", "p1i1"]
    assert page.goto_pages == [1, 2]


# ----------------------------------------------------------------------
# _iter_parallel_reviews
# ----------------------------------------------------------------------


class _FakeAdapter:
    def __init__(
        self,
        reviews: list[str],
        error: Exception | None = None,
    ) -> None:
        self.reviews = reviews
        self.error = error
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        self.last_product_title: str | None = None

    async def iter_reviews(self, url: str) -> Any:
        for review_id in self.reviews:
            yield review_id
        if self.error is not None:
            raise self.error


def _merge(
    adapters: list[_FakeAdapter],
) -> tuple[list[str], BaseException | None]:
    async def run() -> tuple[list[str], BaseException | None]:
        ids: list[str] = []
        try:
            async for review in _iter_parallel_reviews(
                adapters, PRODUCT_URL,
            ):
                ids.append(review)
        except BaseException as exc:  # noqa: BLE001
            return ids, exc
        return ids, None

    return asyncio.run(run())


def test_parallel_merge_interleaves_and_tolerates_partial_death() -> None:
    ids, exc = _merge([
        _FakeAdapter(["a1"], error=YandexCaptchaError("капча")),
        _FakeAdapter(["b1", "b2"]),
    ])
    assert sorted(ids) == ["a1", "b1", "b2"]
    assert exc is None


def test_parallel_merge_raises_only_when_every_session_dies_barren() -> None:
    ids, exc = _merge([
        _FakeAdapter([], error=YandexCaptchaError("капча 1")),
        _FakeAdapter([], error=YandexCaptchaError("капча 2")),
    ])
    assert ids == []
    assert isinstance(exc, YandexCaptchaError)


def test_parallel_merge_partial_yield_beats_total_failure() -> None:
    # One session delivered reviews before dying — that is a
    # partial success, not an error run.
    ids, exc = _merge([
        _FakeAdapter(["a1"], error=YandexCaptchaError("капча")),
        _FakeAdapter([], error=YandexCaptchaError("капча")),
    ])
    assert ids == ["a1"]
    assert exc is None


# ----------------------------------------------------------------------
# Stealth layer: referer chain, click-first navigation, scroll
# ----------------------------------------------------------------------


def test_referer_chain_for_deep_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage({
        p: [_card(p, 0)] for p in (1, 2, 3)
    })  # page 4 missing → empty → the walk ends
    _patch_import(monkeypatch, page)
    _collect(_transport())
    reviews_gtos = [
        call
        for call in page.goto_calls
        if "/reviews" in call["url"]
    ]
    assert len(reviews_gtos) == 4  # pages 1-3 + the empty page 4
    # Page 1 comes from the card; every deeper page refers to its
    # predecessor — never the card.
    assert reviews_gtos[0]["referer"].endswith("/12345678")
    assert reviews_gtos[1]["referer"].endswith("page=1")
    assert reviews_gtos[2]["referer"].endswith("page=2")
    assert reviews_gtos[3]["referer"].endswith("page=3")


def test_pager_click_used_and_exact_param_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # page=20 is the decoy: the pager click must match page=2
    # EXACTLY, and a successful click means no goto for page 2.
    page = _FakePage(
        {
            1: [_card(1, 0)],
            2: [_card(2, 0)],
            3: [],  # end of list
        },
        pager_links=[
            "/card/smartfon-x/12345678/reviews?page=20",
            "/card/smartfon-x/12345678/reviews?page=2",
        ],
    )
    _patch_import(monkeypatch, page)
    ids = _collect(_transport())
    assert "p2i0" in ids
    goto_review_urls = [
        call["url"]
        for call in page.goto_calls
        if "/reviews" in call["url"]
    ]
    assert any(u.endswith("page=1") for u in goto_review_urls)
    assert not any(
        u.endswith("page=2") for u in goto_review_urls
    )


def test_missing_pager_link_falls_back_to_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(
        {1: [_card(1, 0)], 2: []},
        pager_links=[],  # the pager never rendered
    )
    _patch_import(monkeypatch, page)
    ids = _collect(_transport())
    assert ids == ["p1i0"]
    assert page.goto_pages == [1, 2]


def test_scroll_stride_varies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage({1: [_card(1, 0)]})
    _patch_import(monkeypatch, page)
    transport = _transport(
        scroll_step=1600, scroll_pause_ms=1,
    )

    async def run() -> None:
        for _ in range(40):
            await transport._scroll_once(page)

    asyncio.run(run())
    deltas = [d for _, d in page.wheel_events]
    assert deltas
    # Not a metronome: many distinct strides within the humane
    # envelope (0.5..1.4 steps down, occasional short reverse).
    assert len(set(deltas)) > 5
    assert all(-420 <= d <= 2240 for d in deltas)


def test_warmup_reads_the_card_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage({1: [_card(1, 0)], 2: []})
    _patch_import(monkeypatch, page)
    _collect(_transport())
    card_scrolls = [
        delta
        for page_no, delta in page.wheel_events
        if page_no == 0
    ]
    assert card_scrolls  # the warmup scrolled the CARD page
    # Forward reading scrolls in the humane envelope; the reading
    # simulation also throws in occasional short REVERSE nudges
    # (a user scrolling back up a line — an antibot signal
    # _simulate_reading_behavior adds on purpose).
    forward = [d for d in card_scrolls if d > 0]
    reverse = [d for d in card_scrolls if d < 0]
    assert forward
    assert all(350 <= d <= 1100 for d in forward)
    assert all(-200 <= d < 0 for d in reverse)


# ----------------------------------------------------------------------
# The counter probe that sizes the parallel page budget
# ----------------------------------------------------------------------


def _probe_args() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        url=PRODUCT_URL,
        timeout_ms=1_000,
        settle_ms=0,
        no_humanize=True,
        save_cookies=None,
    )


async def test_probe_sizes_page_budget_from_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """47 reviews ~ 5 pages + 1 margin page per session (3) = 8."""
    from marketplace_maps_parser import collectors

    async def fake_probe(self: Any, url: str) -> int:
        return 47

    monkeypatch.setattr(
        YandexBrowserTransport, "fetch_total_count", fake_probe,
    )

    pages = await collectors._probe_yandex_page_count(
        _probe_args(),
        parallel=3,
        proxy=None,
        cookies=None,
        debug_dir="debug_test_probe",
    )
    assert pages == 8


async def test_probe_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe killed by a captcha must return None so the caller
    downgrades to a single session instead of guessing a range."""
    from marketplace_maps_parser import collectors

    async def raising_probe(self: Any, url: str) -> int:
        raise YandexCaptchaError("капча пережила лестницу")

    monkeypatch.setattr(
        YandexBrowserTransport,
        "fetch_total_count",
        raising_probe,
    )

    pages = await collectors._probe_yandex_page_count(
        _probe_args(),
        parallel=2,
        proxy=None,
        cookies=None,
        debug_dir="debug_test_probe",
    )
    assert pages is None
