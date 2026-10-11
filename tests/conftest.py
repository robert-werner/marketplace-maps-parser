"""Shared test fixtures for marketplace-maps-parser."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger


# ---------------------------------------------------------------------------
# Logging – prevent loguru from installing its own root handler during tests
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="session")
def _loguru_no_stderr() -> None:
    """Disable loguru's auto-install so tests aren't flooded."""
    logger.remove()


# ---------------------------------------------------------------------------
# Lightweight mocks for browser / HTTP layer
# ---------------------------------------------------------------------------

class PageMock(MagicMock):
    """Minimal Playwright Page mock for transport tests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.evaluate = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.wait_for_load_state = AsyncMock()
        self.get_by_selector = MagicMock()
        self.on = MagicMock(return_value=MagicMock(remove=lambda: None))
        self.close = AsyncMock()

    # Allow chaining with .get_by_selector(...).click()
    def get_by_selector(self, *args: Any, **kwargs: Any) -> MagicMock:
        el = MagicMock()
        el.click = AsyncMock()
        el.wait_for_state = AsyncMock()
        return el


class BrowserContextMock(MagicMock):
    """Stub for Playwright BrowserContext."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.new_page = AsyncMock(return_value=PageMock())
        self.close = AsyncMock()


class BrowserMock(MagicMock):
    """Stub for Playwright Browser."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.context = MagicMock(return_value=BrowserContextMock())
        self.close = AsyncMock()


@pytest.fixture
def page() -> MagicMock:
    """A fresh PageMock with cleared history."""
    p = PageMock()
    p.reset_mock()
    return p


@pytest.fixture
def browser() -> BrowserMock:
    return BrowserMock()


@pytest.fixture
def context() -> BrowserContextMock:
    return BrowserContextMock()


# ---------------------------------------------------------------------------
# Generic async iterator fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def async_iter_fixture() -> Any:
    """Return a list so tests can create AsyncIterator via __aiter__."""
    return []


# ---------------------------------------------------------------------------
# Disable loguru at module level — prevents "cannot configure once" errors
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _configure_loguru_test() -> None:
    """Ensure loguru is removed (no stderr) before any test runs."""
    logger.remove()
