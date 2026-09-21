"""Tests for retry behavior on Playwright errors.

When invisible-playwright's underlying Playwright session aborts an
operation (typically: ``Page.evaluate: The operation was aborted``,
``Page.goto: Timeout``, or CDP connection drops), the exception class
is ``invisible_playwright._pw._impl._errors.Error``.

These tests verify that:

- ``_retryable_errors()`` includes ``RuntimeError``, ``TimeoutError``,
  ``asyncio.TimeoutError``, and (if installed) Playwright's ``Error``.
- ``_fetch_json_with_retry`` retries when the inner fetch raises a
  Playwright ``Error`` (simulated with the real class so isinstance
  matches work).
- ``_fetch_json_with_retry`` does NOT retry on a foreign exception
  type (``ValueError``), it propagates immediately.
- ``_goto_with_retry`` retries ``page.goto`` calls that raise
  Playwright ``Error``.
- ``_goto_with_retry`` does NOT retry on non-retryable exceptions.

The tests use a fake ``Error`` class when the real Playwright library
is not importable in the test environment (CI without browser stack).
"""
from __future__ import annotations

import asyncio

import pytest

from infrastructure.transports import browser_json as bj_module
from infrastructure.transports.browser_json import (
    BrowserJsonTransport,
)

# Cache the real asyncio.sleep so test monkeypatches can call it
# without infinite recursion.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(*args, **kwargs):
    """No-op replacement for ``asyncio.sleep`` so backoff doesn't wait."""
    return None


def _get_playwright_error_class():
    """Return the real Playwright Error class if available, else
    a local stand-in with the same qualified name (so isinstance
    matches work when retry_on includes the real class).

    In a properly installed environment (with invisible-playwright)
    we use the real class. In CI / minimal envs without the browser
    stack, we patch ``_get_retryable_errors`` to use our stand-in.
    """
    try:
        from invisible_playwright._pw._impl._errors import Error
        return Error
    except ImportError:
        # Stand-in we use in tests
        class _FakePlaywrightError(Exception):
            pass

        _FakePlaywrightError.__module__ = (
            "invisible_playwright._pw._impl._errors"
        )
        _FakePlaywrightError.__name__ = "Error"
        _FakePlaywrightError.__qualname__ = "Error"
        return _FakePlaywrightError


class _FakePage:
    """Stand-in page object — methods are monkeypatched per-test."""
    pass


# ---------------------------------------------------------------------------
# _retryable_errors()
# ---------------------------------------------------------------------------


def test_retryable_errors_includes_runtime_and_timeout():
    errs = bj_module._retryable_errors()
    assert RuntimeError in errs
    assert TimeoutError in errs
    assert asyncio.TimeoutError in errs


def test_retryable_errors_includes_playwright_error_when_installed():
    """If invisible-playwright is installed, its Error class is in
    the retryable set."""
    try:
        from invisible_playwright._pw._impl._errors import Error
    except ImportError:
        pytest.skip(
            "invisible-playwright not installed in this env; "
            "the test only makes sense when it is."
        )
    assert Error in bj_module._retryable_errors()


# ---------------------------------------------------------------------------
# _fetch_json_with_retry — Playwright Error retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_with_retry_retries_on_playwright_error(monkeypatch):
    """A Playwright Error on attempts 1 and 2, success on attempt 3."""
    transport = BrowserJsonTransport()

    PlaywrightError = _get_playwright_error_class()
    call_count = {"n": 0}

    async def fake_inner_fetch(*, page, internal_path):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise PlaywrightError(
                "Page.evaluate: The operation was aborted."
            )
        return {"ok": True, "attempt": call_count["n"]}

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        fake_inner_fetch,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # If invisible-playwright is NOT installed, the real Error class
    # is not in retry_on. Patch _get_retryable_errors to include our
    # stand-in class so the retry triggers.
    try:
        from invisible_playwright._pw._impl._errors import Error  # noqa
    except ImportError:
        monkeypatch.setattr(
            bj_module,
            "_retryable_errors",
            lambda: (
                RuntimeError, TimeoutError,
                asyncio.TimeoutError, PlaywrightError,
            ),
        )

    result = await transport._fetch_json_with_retry(
        page=_FakePage(),
        internal_path="/p",
        attempts=5,
        label="test",
    )

    assert result == {"ok": True, "attempt": 3}
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_fetch_with_retry_propagates_non_retryable(monkeypatch):
    """A ValueError is not in retry_on — it should propagate on the
    first attempt, without consuming the retry budget."""
    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    async def fake_inner_fetch(*, page, internal_path):
        call_count["n"] += 1
        raise ValueError("totally unrelated error")

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        fake_inner_fetch,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(ValueError, match="totally unrelated error"):
        await transport._fetch_json_with_retry(
            page=_FakePage(),
            internal_path="/p",
            attempts=5,
            label="test",
        )

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_fetch_with_retry_exhausts_on_continuous_playwright_error(
    monkeypatch,
):
    """All attempts raise Playwright Error → last one propagates."""
    transport = BrowserJsonTransport()

    PlaywrightError = _get_playwright_error_class()
    call_count = {"n": 0}

    async def always_fail(*, page, internal_path):
        call_count["n"] += 1
        raise PlaywrightError(f"always abort #{call_count['n']}")

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        always_fail,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    try:
        from invisible_playwright._pw._impl._errors import Error  # noqa
    except ImportError:
        monkeypatch.setattr(
            bj_module,
            "_retryable_errors",
            lambda: (
                RuntimeError, TimeoutError,
                asyncio.TimeoutError, PlaywrightError,
            ),
        )

    with pytest.raises(PlaywrightError, match="always abort"):
        await transport._fetch_json_with_retry(
            page=_FakePage(),
            internal_path="/p",
            attempts=3,
            label="test",
        )

    assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# _goto_with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goto_with_retry_succeeds_after_transient_abort(monkeypatch):
    """page.goto raises 'operation aborted' twice, succeeds on third.

    _goto_with_retry now recreates the page on each attempt, so the
    page_factory is called 3 times total.
    """
    transport = BrowserJsonTransport()

    PlaywrightError = _get_playwright_error_class()
    call_count = {"n": 0}

    class _FakePageWithGoto:
        async def goto(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise PlaywrightError(
                    "Page.goto: The operation was aborted."
                )
            return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    try:
        from invisible_playwright._pw._impl._errors import Error  # noqa
    except ImportError:
        monkeypatch.setattr(
            bj_module,
            "_retryable_errors",
            lambda: (
                RuntimeError, TimeoutError,
                asyncio.TimeoutError, PlaywrightError,
            ),
        )

    async def page_factory():
        return _FakePageWithGoto()

    page = await transport._goto_with_retry(
        page_factory=page_factory,
        reviews_url="https://www.ozon.ru/product/foo-123/reviews?page=1",
        attempts=5,
        label="test goto",
    )

    # page_factory was called 3 times (one per attempt) — the 3rd
    # attempt's goto succeeded.
    assert call_count["n"] == 3
    assert isinstance(page, _FakePageWithGoto)


@pytest.mark.asyncio
async def test_goto_with_retry_exhausts_attempts(monkeypatch):
    """page.goto keeps aborting → final attempt's error propagates."""
    transport = BrowserJsonTransport()

    PlaywrightError = _get_playwright_error_class()

    class _FakePageWithGoto:
        async def goto(self, *args, **kwargs):
            raise PlaywrightError("Page.goto: timeout")

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    try:
        from invisible_playwright._pw._impl._errors import Error  # noqa
    except ImportError:
        monkeypatch.setattr(
            bj_module,
            "_retryable_errors",
            lambda: (
                RuntimeError, TimeoutError,
                asyncio.TimeoutError, PlaywrightError,
            ),
        )

    async def page_factory():
        return _FakePageWithGoto()

    with pytest.raises(PlaywrightError, match="timeout"):
        await transport._goto_with_retry(
            page_factory=page_factory,
            reviews_url="https://www.ozon.ru/product/foo-123/reviews?page=1",
            attempts=3,
            label="test goto",
        )


@pytest.mark.asyncio
async def test_goto_with_retry_propagates_non_retryable(monkeypatch):
    """page.goto raises TypeError (a bug, not a transient failure) →
    propagate immediately without retry."""
    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    class _FakePageWithGoto:
        async def goto(self, *args, **kwargs):
            call_count["n"] += 1
            raise TypeError("not a retryable error")

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    async def page_factory():
        return _FakePageWithGoto()

    with pytest.raises(TypeError, match="not a retryable error"):
        await transport._goto_with_retry(
            page_factory=page_factory,
            reviews_url="https://www.ozon.ru/product/foo-123/reviews?page=1",
            attempts=5,
            label="test goto",
        )

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_goto_with_retry_attempts_le_1_skips_retry(monkeypatch):
    """Fast path: attempts=1 calls page.goto directly without going
    through retry_async."""
    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    class _FakePageWithGoto:
        async def goto(self, *args, **kwargs):
            call_count["n"] += 1
            return None

    async def page_factory():
        return _FakePageWithGoto()

    page = await transport._goto_with_retry(
        page_factory=page_factory,
        reviews_url="https://www.ozon.ru/product/foo-123/reviews?page=1",
        attempts=1,
        label="test goto",
    )

    assert call_count["n"] == 1
    assert isinstance(page, _FakePageWithGoto)
