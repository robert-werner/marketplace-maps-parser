"""Async retry helpers for transient failures during scraping.

The main entrypoint is ``retry_async``, which wraps a coroutine factory
with exponential backoff + jitter. It is intentionally small and
dependency-free so it can be used both inside transports (per-page
fetch retries) and at the adapter level (whole-strategy retries).
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from shared.logging import get_logger

T = TypeVar("T")

log = get_logger("shared.retry")


def _default_should_retry(
    exc: BaseException,
    *,
    retry_on: tuple[type[BaseException], ...],
) -> bool:
    """Return True if ``exc`` is a kind of error worth retrying."""
    return isinstance(exc, retry_on)


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    factor: float = 2.0,
    jitter: float = 0.25,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[BaseException, int, int], None] | None = None,
    label: str = "operation",
) -> T:
    """Call ``factory()`` up to ``attempts`` times with exponential backoff.

    Parameters
    ----------
    factory
        Zero-arg callable returning an awaitable. A factory is used
        (instead of accepting a coroutine directly) so each retry can
        re-issue the underlying network call.
    attempts
        Maximum number of attempts including the first one. ``attempts=3``
        means at most 2 retries after the first failure.
    base_delay, factor, max_delay
        Exponential backoff parameters. Delay for attempt ``n`` is
        ``min(base_delay * factor**(n-1), max_delay)`` seconds.
    jitter
        Relative jitter in [0, jitter]. The actual delay is multiplied
        by ``(1 + random.uniform(-jitter, jitter))`` so the resulting
        delay stays in ``[-jitter, +jitter]`` of the base value. Set to
        0 to disable jitter.
    retry_on
        Tuple of exception types that trigger a retry. Other exceptions
        propagate immediately.
    on_retry
        Optional callback ``(exc, attempt, max_attempts) -> None``
        invoked before each retry sleep. Useful for logging.
    label
        Human-readable label used in default log messages.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc

            if not _default_should_retry(
                exc, retry_on=retry_on
            ):
                raise

            if attempt >= attempts:
                log.warning(
                    "{}: exhausted {} attempts: {}",
                    label, attempts, exc,
                )
                raise

            delay = min(
                base_delay * (factor ** (attempt - 1)),
                max_delay,
            )
            if jitter > 0:
                delay *= 1 + random.uniform(
                    -jitter, jitter,
                )
            delay = max(0.0, delay)

            log.warning(
                "{}: attempt {}/{} failed: {} — retry in {:.2f}s",
                label, attempt, attempts, exc, delay,
            )

            if on_retry is not None:
                on_retry(exc, attempt, attempts)

            await asyncio.sleep(delay)

    # Should be unreachable — last attempt either returned or raised.
    assert last_exc is not None
    raise last_exc


async def sleep_with_jitter(
    base_seconds: float,
    *,
    jitter: float = 0.4,
    min_seconds: float = 0.0,
) -> None:
    """Sleep for ``base_seconds * (1 ± jitter)`` but at least ``min_seconds``.

    Used between page fetches to add human-like timing variance.
    """
    if base_seconds <= 0:
        return
    delay = base_seconds
    if jitter > 0:
        delay *= 1 + random.uniform(-jitter, jitter)
    delay = max(delay, min_seconds)
    await asyncio.sleep(delay)
