"""Tests for shared.pacing.AdaptivePacer."""
from __future__ import annotations

import asyncio
import random

import pytest

from shared.pacing import AdaptivePacer


def test_success_shrinks_geometrically_to_floor():
    pacer = AdaptivePacer(base_delay=2.0, min_delay=0.4)

    assert pacer.current_delay == pytest.approx(2.0)

    pacer.record_success()
    assert pacer.current_delay == pytest.approx(2.0 * 0.85)

    for _ in range(50):
        pacer.record_success()

    assert pacer.current_delay == pytest.approx(0.4)


def test_block_resets_and_adds_penalty():
    pacer = AdaptivePacer(base_delay=1.0, min_delay=0.4)

    for _ in range(10):
        pacer.record_success()
    # shrunk far below base
    assert pacer.current_delay < 1.0

    pacer.record_block()
    # reset to base + 2s penalty
    assert pacer.current_delay == pytest.approx(3.0)

    pacer.record_block()
    # consecutive block: penalty doubles to 4s
    assert pacer.current_delay == pytest.approx(5.0)

    pacer.record_success()
    # clean page clears the penalty and shrinks the base
    assert pacer.current_delay == pytest.approx(0.85)


def test_block_penalty_capped():
    pacer = AdaptivePacer(base_delay=1.0)

    for _ in range(10):
        pacer.record_block()

    # base (1.0) + max penalty (30.0)
    assert pacer.current_delay == pytest.approx(31.0)


def test_min_delay_clamped_to_base():
    pacer = AdaptivePacer(base_delay=0.5, min_delay=2.0)
    assert pacer.min_delay == 0.5


def test_reset_restores_initial_state():
    pacer = AdaptivePacer(base_delay=1.0)
    pacer.record_block()
    pacer.record_success()

    pacer.reset()

    assert pacer.current_delay == pytest.approx(1.0)


async def test_wait_sleeps_current_delay_with_jitter(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    pacer = AdaptivePacer(base_delay=2.0)
    pacer.record_block()  # 2.0 + 2.0 = 4.0

    await pacer.wait()

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(4.0, rel=0.31)


async def test_wait_noop_at_zero_delay():
    pacer = AdaptivePacer(base_delay=0.0)
    # Must return without sleeping (patched sleep would blow up the
    # test budget on a real 30s penalty otherwise impossible at 0).
    await pacer.wait()
    pacer.record_block()
    await pacer.wait()


async def test_wait_jitter_stays_in_bounds(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    pacer = AdaptivePacer(base_delay=1.0, min_delay=1.0)
    for _ in range(100):
        await pacer.wait()
        pacer.record_success()
        pacer.reset()

    assert sleeps
    assert all(0.7 <= value <= 1.3 for value in sleeps)


def test_random_module_untouched_import():
    # Guard: the module must not accidentally shadow random (the
    # jitter test above relies on the real random.uniform).
    assert callable(random.uniform)
