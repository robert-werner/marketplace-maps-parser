"""Bounded fan-in of independent review sessions, with failure propagation."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from shared.async_iterators import closing_iterator


class PartialCollectionError(RuntimeError):
    """A parallel run yielded data but at least one session failed."""


async def _iter_parallel_reviews(
    adapters: list[Any],
    url: str,
) -> AsyncIterator[Any]:
    """Fan the review streams of N adapters (one browser session
    each, disjoint page ranges) into a single queue.

    Keep delivered reviews and drain healthy siblings, then surface every
    session failure so a partial run cannot look complete."""
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
    failures: list[BaseException] = []
    yielded = 0

    async def pump(adapter: Any) -> None:
        try:
            async with closing_iterator(adapter.iter_reviews(url)) as stream:
                async for review in stream:
                    await queue.put(review)
        except Exception as exc:
            failures.append(exc)
            print(f"[warning] Я.Маркет: сессия остановилась: {exc}")

    tasks = [
        asyncio.create_task(pump(adapter))
        for adapter in adapters
    ]

    async def finisher() -> None:
        # return_exceptions: a late sibling failure must not turn
        # into an unretrieved-task-exception warning.
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    finisher_task = asyncio.create_task(finisher())
    try:
        while True:
            review = await queue.get()
            if review is None:
                break
            yielded += 1
            yield review
        if not yielded and failures:
            raise failures[0]
        if yielded and failures:
            raise PartialCollectionError(
                f"{len(failures)} из {len(adapters)} "
                "параллельных сессий завершились с ошибкой"
            ) from failures[0]
    finally:
        finisher_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(
            finisher_task, *tasks, return_exceptions=True,
        )


class _ParallelYandexSessions:
    """Adapter stand-in for ``--parallel-sessions`` (yandex): N
    one-range browser sessions fanned into a single review stream.

    Exposes the aggregate ``last_*`` totals so both output formats
    and the summary print work unchanged."""

    def __init__(self, adapters: list[Any]) -> None:
        self._adapters = adapters
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        self.last_product_title: str | None = None

    async def iter_reviews(self, url: str) -> AsyncIterator[Any]:
        async with closing_iterator(_iter_parallel_reviews(
            self._adapters, url,
        )) as stream:
            async for review in stream:
                # Refresh the totals on every review (the same pattern
                # as the single-session adapter: they must survive an
                # early --max-reviews break).
                counts = [
                    adapter.last_total_count
                    for adapter in self._adapters
                    if adapter.last_total_count is not None
                ]
                self.last_total_count = (
                    max(counts) if counts else None
                )
                ratings = [
                    adapter.last_average_rating
                    for adapter in self._adapters
                    if adapter.last_average_rating is not None
                ]
                self.last_average_rating = (
                    ratings[0] if ratings else None
                )
                titles = [
                    adapter.last_product_title
                    for adapter in self._adapters
                    if adapter.last_product_title
                ]
                self.last_product_title = (
                    titles[0] if titles else None
                )
                yield review


