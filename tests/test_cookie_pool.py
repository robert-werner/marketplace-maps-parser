"""Tests for CookiePool: rotation, jar blocking, peek, exhaustion.

Mirror of the proxy-pool semantics, but for cookie jars (logged-in
browser sessions).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from infrastructure.transports.cookie_pool import CookiePool


def _playwright_jar(session_id: str) -> list[dict]:
    return [
        {
            "name": "session_id",
            "value": session_id,
            "domain": ".ozon.ru",
            "path": "/",
            "expires": 1893456000.0,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
    ]


def _entries(*jars: list[dict]) -> list[tuple[Path | None, list[dict]]]:
    return [(None, jar) for jar in jars]


def _jar_dir(tmp_path: Path, jars: list[list[dict]], name: str = "jars") -> Path:
    root = tmp_path / name
    root.mkdir()
    for i, jar in enumerate(jars):
        (root / f"session_{i}.json").write_text(
            json.dumps(jar, ensure_ascii=False), encoding="utf-8",
        )
    return root


def test_round_robin_without_blocking():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    assert pool.size == 2
    assert pool.next_jar() == j1
    assert pool.next_jar() == j2
    assert pool.next_jar() == j1
    assert pool.next_jar() == j2


def test_round_robin_by_source_files():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries([
        (Path("/tmp/a.json"), j1),
        (Path("/tmp/b.json"), j2),
    ])
    assert pool.next() == Path("/tmp/a.json")
    assert pool.next() == Path("/tmp/b.json")
    assert pool.next() == Path("/tmp/a.json")


def test_mark_blocked_by_contents():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    pool.mark_blocked(j1)
    # blocked jar skipped forever (as long as j2 lives); the pool
    # only exhausts when every jar is blocked.
    for _ in range(6):
        assert pool.next_jar() == j2
    assert pool.available_count == 1
    assert not pool.all_blocked()
    pool.mark_blocked(j2)
    assert pool.all_blocked()
    assert pool.next_jar() is None


def test_mark_blocked_by_source_file(tmp_path):
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    root = _jar_dir(tmp_path, [j1, j2])
    pool = CookiePool.from_directory(root)
    assert pool.size == 2
    jar1_path = root / "session_0.json"
    pool.mark_blocked(jar1_path)
    pool.mark_blocked(jar1_path)  # idempotent
    assert pool.available_count == 1
    assert pool.next() == (root / "session_1.json")
    assert pool.next() == (root / "session_1.json")


def test_mark_blocked_with_unknown_source_is_noop():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    pool.mark_blocked(Path("/does/not/exist/in/pool.json"))
    assert pool.available_count == 2


def test_exhaustion_when_all_blocked():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    pool.mark_blocked(j1)
    pool.mark_blocked(j2)
    assert pool.all_blocked()
    assert pool.available_count == 0
    assert pool.next() is None
    assert pool.next_jar() is None
    assert pool.peek_jar() is None


def test_reset_blocked_unblocks():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    pool.mark_blocked(j1)
    pool.reset_blocked()
    assert pool.available_count == 2
    assert not pool.all_blocked()
    assert pool.next_jar() is not None


def test_peek_jar_does_not_advance_pointer():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    assert pool.peek_jar() == j1  # peek, pointer unchanged
    assert pool.next_jar() == j1  # still j1
    pool.mark_blocked(j1)
    assert pool.peek_jar() == j2
    assert pool.next_jar() == j2
    assert pool.peek_jar() == j2


def test_duplicate_jars_are_deduped():
    j = _playwright_jar("dup")
    pool = CookiePool.from_entries([
        (None, j),
        (None, list(j)),
        (None, j),
    ])
    assert pool.size == 1
    assert pool.next_jar() == j


def test_empty_pool_is_rejected():
    with pytest.raises(ValueError):
        CookiePool.from_entries([])
    with pytest.raises(ValueError):
        CookiePool([(None, []), (None, [])])


def test_from_directory_skips_unparseable_and_empty(tmp_path):
    j1 = _playwright_jar("ok")
    root = _jar_dir(tmp_path, [j1])
    (root / "bad.json").write_text("{ not json", encoding="utf-8")
    (root / "empty.json").write_text("[]", encoding="utf-8")
    (root / "junk.txt").write_text("hello", encoding="utf-8")
    pool = CookiePool.from_directory(root)
    assert pool.size == 1


def test_from_list_in_memory():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_list([j1, j2])
    assert pool.size == 2
    assert pool.next_jar() == j1
    assert pool.next_jar() == j2
    # next() also consumes a rotation step (advances the pointer) and
    # returns None for in-memory pools (no source files).
    assert pool.next() is None
    assert pool.next_jar() == j2  # pointer wrapped around


def test_next_jar_async_equivalent():
    import asyncio

    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    assert asyncio.run(pool.next_jar_async()) == j1
    assert asyncio.run(pool.next_jar_async()) == j2


def test_get_stats():
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    assert pool.get_stats() == {"total": 2, "available": 2, "blocked": 0}
    pool.mark_blocked(j1)
    assert pool.get_stats() == {"total": 2, "available": 1, "blocked": 1}


def test_thread_safety_of_next():
    """Concurrent next_jar() from threads must not crash and must not
    hand the same jar twice from a single pointer advance."""
    import threading

    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2, j1, j2))
    results: list[list[dict]] = []
    lock = threading.Lock()

    def worker():
        for _ in range(2):
            jar = pool.next_jar()
            if jar is not None:
                with lock:
                    results.append(jar)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    values = {c["value"] for r in results for c in r}
    assert values == {"one", "two"}


def test_ozon_retry_pattern_peek_mark_blocked():
    """Simulates the _collect_ozon_cookie_retries pattern:
    peek -> run -> mark_blocked on failure -> next run skips the jar.
    This is the exact shape collectors.py uses, tested against the
    pool directly (no browser)."""
    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    # run 1: peek the jar this run will consume
    jar = pool.peek_jar()
    assert jar == j1
    # run fails -> mark blocked
    pool.mark_blocked(jar)  # type: ignore[arg-type]
    # run 2 must not reuse j1
    jar2 = pool.peek_jar()
    assert jar2 is j2
    assert jar2 is not jar
    assert pool.next_jar() == j2


@pytest.mark.asyncio
async def test_next_jar_async_with_two_calls():
    """Async consumers must not starve sync consumers."""
    import asyncio

    j1, j2 = _playwright_jar("one"), _playwright_jar("two")
    pool = CookiePool.from_entries(_entries(j1, j2))
    first = pool.next_jar()  # sync
    second = await pool.next_jar_async()
    assert (first, second) == (j1, j2)
