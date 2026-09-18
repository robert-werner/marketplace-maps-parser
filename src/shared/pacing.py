# src/shared/pacing.py
"""Adaptive inter-page pacing for long scraping runs.

A fixed inter-page delay is a bad trade on both ends: too long and a
clean run wastes minutes waiting for nothing, too short and the
antibot starts challenging. :class:`AdaptivePacer` starts from the
user-supplied ``base_delay`` and adapts:

- every clean page shrinks the delay geometrically (factor
  ``_SHRINK``), down to ``min_delay`` (default 0.4s);
- every reported antibot/challenge event resets the delay to
  ``base_delay`` and adds a one-off penalty (``_PENALTY_S`` extra,
  doubled for each consecutive event) so runs that are being noticed
  back off hard;
- ``wait()`` jitters the sleep (±30%) so the delay sequence never
  becomes a fixed rhythm the antibot can fingerprint.

All four Ozon transports create ``self._pacer = AdaptivePacer(
base_delay=page_delay_seconds)`` inside ``iter_all_ozon_reviews`` and
drive it through ``record_success()`` / ``wait()`` / ``record_block()``
(see ``transports/base.py::_notify_pacer_block``).
"""
from __future__ import annotations

import asyncio
import random

# Geometric shrink factor applied after each clean page.
_SHRINK = 0.85

# One-off extra penalty after a block, in seconds. Doubles for each
# consecutive block (2 → 4 → 8 …, capped at _MAX_PENALTY_S) and resets
# after the first clean page.
_PENALTY_S = 2.0
_MAX_PENALTY_S = 30.0

# ±30% jitter on every sleep, same envelope as shared.retry.
_JITTER = 0.3


class AdaptivePacer:
    """Adaptive delay between pages of one scraping run.

    Parameters
    ----------
    base_delay:
        Starting inter-page delay in seconds (the CLI's
        ``--page-delay-seconds``).
    min_delay:
        Floor the delay never shrinks below.
    """

    def __init__(
        self,
        *,
        base_delay: float | None = 1.0,
        min_delay: float = 0.4,
    ) -> None:
        if base_delay is None:
            base_delay = 1.0
        self.base_delay = max(0.0, float(base_delay))
        self.min_delay = max(0.0, float(min_delay))
        if self.min_delay > self.base_delay:
            self.min_delay = self.base_delay
        self._current = self.base_delay
        self._penalty = 0.0
        self._consecutive_blocks = 0

    @property
    def current_delay(self) -> float:
        """Delay that the next ``wait()`` will sleep (before jitter)."""
        return self._current + self._penalty

    def record_success(self) -> None:
        """A page completed without any antibot event: shrink."""
        self._consecutive_blocks = 0
        self._penalty = 0.0
        self._current = max(
            self.min_delay, self._current * _SHRINK,
        )

    def record_block(self) -> None:
        """An antibot/challenge event was detected: back off."""
        self._consecutive_blocks += 1
        self._current = self.base_delay
        self._penalty = min(
            _PENALTY_S * (2 ** (self._consecutive_blocks - 1)),
            _MAX_PENALTY_S,
        )

    async def wait(self) -> None:
        """Sleep the current delay (with jitter). No-op at zero."""
        delay = self.current_delay
        if delay <= 0:
            return
        jittered = delay * (1.0 + random.uniform(-_JITTER, _JITTER))
        jittered = max(0.0, jittered)
        await asyncio.sleep(jittered)

    def reset(self) -> None:
        """Return to the initial state (start of a new stream)."""
        self._current = self.base_delay
        self._penalty = 0.0
        self._consecutive_blocks = 0


__all__ = ["AdaptivePacer"]
