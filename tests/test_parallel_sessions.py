"""Tests for parallel-sessions helpers and per-page speedups
(asset blocking, adaptive hydration wait)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from infrastructure.transports.public_page import PublicPageTransport
from marketplace_maps_parser.parallel_sessions import (
    merge_jsonl_dedup,
    split_page_range,
)


# ---------------------------------------------------------------------------
# split_page_range
# ---------------------------------------------------------------------------


def test_split_even():
    assert split_page_range(1, 30, 3) == [(1, 10), (11, 10), (21, 10)]


def test_split_remainder_to_first_chunks():
    # 31 страница на 3 сессии: первые получают +1
    assert split_page_range(1, 31, 3) == [(1, 11), (12, 10), (22, 10)]


def test_split_more_sessions_than_pages():
    assert split_page_range(1, 2, 5) == [(1, 1), (2, 1)]


def test_split_custom_start_page():
    assert split_page_range(50, 10, 2) == [(50, 5), (55, 5)]


def test_split_invalid():
    with pytest.raises(ValueError):
        split_page_range(1, 10, 0)
    with pytest.raises(ValueError):
        split_page_range(1, 0, 2)


# ---------------------------------------------------------------------------
# merge_jsonl_dedup
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, reviews: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in reviews:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_merge_dedups_across_parts(tmp_path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    out = tmp_path / "out.jsonl"
    _write_jsonl(a, [
        {"review_id": "r1", "text": "x"},
        {"review_id": "r2", "text": "y"},
    ])
    _write_jsonl(b, [
        {"review_id": "r2", "text": "y"},  # дубль из части a
        {"review_id": "r3", "text": "z"},
    ])
    count = merge_jsonl_dedup([a, b], out)
    assert count == 3
    ids = [
        json.loads(line)["review_id"]
        for line in open(out, encoding="utf-8")
    ]
    assert ids == ["r1", "r2", "r3"]


def test_merge_skips_bad_lines_and_missing_parts(tmp_path):
    a = tmp_path / "a.jsonl"
    out = tmp_path / "out.jsonl"
    a.write_text(
        '{"review_id": "r1"}\n'
        "not json at all\n"
        "\n"
        '{"review_id": "r1"}\n',
        encoding="utf-8",
    )
    count = merge_jsonl_dedup(
        [a, tmp_path / "missing.jsonl"], out
    )
    assert count == 1


def test_merge_respects_max_reviews(tmp_path):
    a = tmp_path / "a.jsonl"
    out = tmp_path / "out.jsonl"
    _write_jsonl(
        a, [{"review_id": f"r{i}"} for i in range(10)]
    )
    assert merge_jsonl_dedup([a], out, max_reviews=3) == 3


# ---------------------------------------------------------------------------
# Asset blocker + adaptive hydration
# ---------------------------------------------------------------------------


class _RouteRecordingPage:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []

    def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))


@pytest.mark.asyncio
async def test_resource_blocker_installed_by_default():
    page = _RouteRecordingPage()
    await PublicPageTransport()._install_resource_blocker(page)
    # по одному шаблону на каждое расширение ассетов
    assert len(page.routes) == 10
    assert all(p != "**/*" for p, _ in page.routes)


@pytest.mark.asyncio
async def test_resource_blocker_disabled():
    page = _RouteRecordingPage()
    await PublicPageTransport(
        block_assets=False,
    )._install_resource_blocker(page)
    assert page.routes == []


@pytest.mark.asyncio
async def test_resource_blocker_tolerates_pages_without_route():
    # фейковые страницы не имеют route() — не должно падать
    await PublicPageTransport()._install_resource_blocker(object())


class _HydrationPage:
    """evaluate возвращает 0 (не гидратировано), затем int>0."""

    def __init__(self, answers: list[Any]) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def evaluate(self, expression: str, *args) -> Any:
        self.calls += 1
        if self._answers:
            return self._answers.pop(0)
        return 1


@pytest.mark.asyncio
async def test_hydration_wait_returns_after_svg_appear():
    page = _HydrationPage([0, 0, 5])
    await PublicPageTransport()._wait_for_cards_hydrated(page)
    assert page.calls == 3


@pytest.mark.asyncio
async def test_hydration_wait_falls_through_without_evaluate():
    class _NoEval:
        pass

    # объект без evaluate — исключение внутри → выход сразу
    await PublicPageTransport()._wait_for_cards_hydrated(_NoEval())
