"""Explicit ownership of nested streams, including on early consumer exit."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def closing_iterator[T](
    iterator: AsyncIterator[T],
) -> AsyncIterator[AsyncIterator[T]]:
    """Close a child before its parent/session exits, not during loop shutdown.

    ``async for`` alone does not close a suspended async generator on break
    or GeneratorExit. Waiting for GC can race with asyncio.shutdown_asyncgens
    and tear down the browser while its nested generators are still closing.
    Ordinary AsyncIterator implementations need not implement ``aclose``.
    """
    try:
        yield iterator
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
