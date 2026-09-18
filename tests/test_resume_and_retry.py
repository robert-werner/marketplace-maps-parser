"""Tests for CLI ``--resume`` and the per-page retry helper in the
transport.

These do not require a real browser — they cover the parts that are
testable in pure Python:

- ``_load_existing_reviews`` correctly reads review_ids from a JSONL
  file, tolerates malformed lines, and returns an empty set when the
  file does not exist.
- ``_load_existing_reviews`` after a fake first-run + second-run
  produces only the new reviews (resume dedup).
- ``BrowserJsonTransport._fetch_json_with_retry`` retries on
  ``RuntimeError`` (the kind raised by the inner fetch on HTTP
  non-200) and gives up after the configured number of attempts.
- ``retry_async`` does NOT retry on non-RuntimeError exceptions
  (e.g. ``TypeError``), they propagate immediately.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from marketplace_maps_parser.__main__ import (
    _load_existing_reviews,
)

# Cache the real asyncio.sleep so test monkeypatches can call it
# without infinite recursion.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(*args, **kwargs):
    """No-op replacement for ``asyncio.sleep`` used in retry tests
    so the exponential backoff doesn't actually wait."""
    return None


# ---------------------------------------------------------------------------
# _load_existing_reviews
# ---------------------------------------------------------------------------


def test_load_existing_reviews_missing_file_returns_empty(tmp_path):
    """If the output file does not exist, resume returns empty set."""
    missing = tmp_path / "nonexistent.jsonl"
    assert _load_existing_reviews(missing) == set()


def test_load_existing_reviews_reads_ids(tmp_path):
    """A well-formed JSONL file yields the set of review_ids."""
    path = tmp_path / "reviews.jsonl"
    records = [
        {"review_id": "r1", "rating": 5, "text": "a"},
        {"review_id": "r2", "rating": 4, "text": "b"},
        {"review_id": "r3", "rating": 3, "text": "c"},
    ]
    path.write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False) for r in records
        )
        + "\n",
        encoding="utf-8",
    )

    assert _load_existing_reviews(path) == {"r1", "r2", "r3"}


def test_load_existing_reviews_tolerates_malformed_lines(tmp_path):
    """Bad JSON lines are silently skipped — they do not block the
    resume."""
    path = tmp_path / "reviews.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"review_id": "r1", "rating": 5}),
                "this is not json",
                json.dumps({"review_id": "r2"}),
                "",
                json.dumps({"no_id_here": True}),
                json.dumps({"review_id": "r3"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _load_existing_reviews(path) == {"r1", "r2", "r3"}


def test_load_existing_reviews_handles_blank_file(tmp_path):
    """An empty file returns an empty set, not an error."""
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert _load_existing_reviews(path) == set()


# ---------------------------------------------------------------------------
# End-to-end resume scenario through the CLI helper
# ---------------------------------------------------------------------------


def test_resume_scenario_two_runs_skip_already_collected(tmp_path):
    """Simulate a first-run JSONL, then a second ``--resume`` run that
    only appends new reviews."""
    output = tmp_path / "ozon_reviews.jsonl"

    # First run wrote r1, r2, r3
    first_run = [
        {"review_id": "r1", "rating": 5, "text": "first1"},
        {"review_id": "r2", "rating": 4, "text": "first2"},
        {"review_id": "r3", "rating": 3, "text": "first3"},
    ]
    with output.open("w", encoding="utf-8") as f:
        for r in first_run:
            f.write(
                json.dumps(r, ensure_ascii=False) + "\n"
            )

    # Second run produces r2 (dup), r3 (dup), r4 (new), r5 (new)
    second_run_stream = [
        {"review_id": "r2", "rating": 4, "text": "dup"},
        {"review_id": "r3", "rating": 3, "text": "dup"},
        {"review_id": "r4", "rating": 5, "text": "new4"},
        {"review_id": "r5", "rating": 5, "text": "new5"},
    ]

    seen_ids = _load_existing_reviews(output)
    assert seen_ids == {"r1", "r2", "r3"}

    # Append only the new ones
    with output.open("a", encoding="utf-8") as f:
        for r in second_run_stream:
            if r["review_id"] in seen_ids:
                continue
            seen_ids.add(r["review_id"])
            f.write(
                json.dumps(r, ensure_ascii=False) + "\n"
            )

    # Final file should contain r1, r2, r3, r4, r5 in that order
    final_ids = []
    with output.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            final_ids.append(record["review_id"])

    assert final_ids == ["r1", "r2", "r3", "r4", "r5"]


# ---------------------------------------------------------------------------
# _fetch_json_with_retry (without browser — we monkeypatch the inner
# fetch to simulate failures and successes)
# ---------------------------------------------------------------------------


class FakePage:
    """Just a placeholder so the transport's `page` parameter can be
    passed through without a real Playwright page."""


@pytest.mark.asyncio
async def test_fetch_json_with_retry_succeeds_after_transient_failures(
    monkeypatch,
):
    """The retry wrapper retries on RuntimeError and returns the
    payload once the inner fetch succeeds."""
    from infrastructure.transports.browser_json.transport import (
        BrowserJsonTransport,
    )

    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    async def fake_inner_fetch(*, page, internal_path):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError(
                f"simulated Cloudflare block #{call_count['n']}"
            )
        return {"ok": True, "page": internal_path}

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        fake_inner_fetch,
    )

    # Speed up the test by patching asyncio.sleep to a no-op so the
    # exponential backoff doesn't actually wait.
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    result = await transport._fetch_json_with_retry(
        page=FakePage(),
        internal_path="/product/foo-12345/reviews?page=1",
        attempts=5,
        label="test",
    )

    assert result == {"ok": True, "page": "/product/foo-12345/reviews?page=1"}
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_fetch_json_with_retry_exhausts_attempts(monkeypatch):
    """When all attempts raise, the last RuntimeError propagates."""
    from infrastructure.transports.browser_json.transport import (
        BrowserJsonTransport,
    )

    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    async def always_fail(*, page, internal_path):
        call_count["n"] += 1
        raise RuntimeError(f"always fails #{call_count['n']}")

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        always_fail,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(RuntimeError, match="always fails"):
        await transport._fetch_json_with_retry(
            page=FakePage(),
            internal_path="/product/foo-12345/reviews?page=1",
            attempts=3,
            label="test",
        )

    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_fetch_json_with_retry_no_retry_on_non_runtime_error(
    monkeypatch,
):
    """TypeError is not in retry_on=(RuntimeError,) — it should
    propagate on the first attempt without consuming the retry budget."""
    from infrastructure.transports.browser_json.transport import (
        BrowserJsonTransport,
    )

    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    async def type_error_fetch(*, page, internal_path):
        call_count["n"] += 1
        raise TypeError("not a RuntimeError")

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        type_error_fetch,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(TypeError, match="not a RuntimeError"):
        await transport._fetch_json_with_retry(
            page=FakePage(),
            internal_path="/p",
            attempts=5,
            label="test",
        )

    # Only one attempt was made — no retry on TypeError
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_fetch_json_with_retry_attempts_le_1_skips_retry(monkeypatch):
    """When attempts <= 1, the inner fetch is called directly without
    going through retry_async — this is the fast path that avoids
    the overhead of setting up retry state for the common case."""
    from infrastructure.transports.browser_json.transport import (
        BrowserJsonTransport,
    )

    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    async def fast_fetch(*, page, internal_path):
        call_count["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        fast_fetch,
    )

    result = await transport._fetch_json_with_retry(
        page=FakePage(),
        internal_path="/p",
        attempts=1,
        label="test",
    )

    assert result == {"ok": True}
    assert call_count["n"] == 1
