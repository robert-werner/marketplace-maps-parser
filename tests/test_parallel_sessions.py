"""Tests for parallel-sessions helpers and per-page speedups
(asset blocking, adaptive hydration wait)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infrastructure.transports.public_page import PublicPageTransport
from marketplace_maps_parser.parallel_sessions import (
    _product_child_command,
    merge_jsonl_dedup,
    read_products_file,
    run_products_parallel,
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


# ---------------------------------------------------------------------------
# Multi-product mode (--products-file)
# ---------------------------------------------------------------------------


def test_read_products_file_filters_comments_and_blanks(tmp_path):
    path = tmp_path / "products.txt"
    path.write_text(
        "# Ozon products\n"
        "https://www.ozon.ru/product/a-111\n"
        "\n"
        "  https://www.ozon.ru/product/b-222  \n"
        "# another comment\n"
        "https://www.ozon.ru/product/c-333\n",
        encoding="utf-8",
    )
    assert read_products_file(path) == [
        "https://www.ozon.ru/product/a-111",
        "https://www.ozon.ru/product/b-222",
        "https://www.ozon.ru/product/c-333",
    ]


def test_read_products_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_products_file(tmp_path / "nope.txt")


def test_read_products_file_empty_raises(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("# only comments\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no URLs"):
        read_products_file(path)


def _products_args(**overrides: Any) -> SimpleNamespace:
    base = dict(
        marketplace="ozon",
        url=None,
        products_file="products.txt",
        products_sessions=2,
        output="out.jsonl",
        transport="public_page",
        strategy="auto",
        retry_attempts=3,
        max_reviews=None,
        cookies="cookies.json",
        timeout_ms=90000,
        settle_ms=3000,
        debug_dir="debug_ozon",
        workers=1,
        no_stealth=False,
        no_humanize=False,
        no_widget_scroll=False,
        randomize_fingerprint=False,
        proxy_list=None,
        proxy=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_product_child_command_forwards_flags():
    args = _products_args()
    cmd = _product_child_command(
        args,
        "https://www.ozon.ru/product/a-111",
        Path("out.p000.jsonl"),
        "http://u:p@1.2.3.4:8080",
    )
    joined = " ".join(cmd)
    assert "--marketplace ozon" in joined
    assert (
        "--url https://www.ozon.ru/product/a-111" in joined
    )
    assert "--transport public_page" in joined
    assert "--strategy auto" in joined
    assert "--proxy http://u:p@1.2.3.4:8080" in joined
    assert "--cookies cookies.json" in joined
    assert "--retry-attempts 3" in joined
    # flags with false values are NOT forwarded
    assert "--no-stealth" not in joined
    assert "--randomize-fingerprint" not in joined
    assert "--workers" not in joined


def test_product_child_command_optional_flags():
    args = _products_args(
        max_reviews=500,
        workers=2,
        no_stealth=True,
        randomize_fingerprint=True,
    )
    cmd = _product_child_command(
        args, "u", Path("p"), None,
    )
    joined = " ".join(cmd)
    assert "--max-reviews 500" in joined
    assert "--workers 2" in joined
    assert "--no-stealth" in joined
    assert "--randomize-fingerprint" in joined
    assert "--proxy" not in joined


@pytest.mark.asyncio
async def test_run_products_parallel_merges_and_dedups(
    tmp_path, monkeypatch,
):
    """One fake child per product: writes its part file, exit 0.
    The supervisor merges parts into --output with dedup."""
    products = tmp_path / "products.txt"
    products.write_text(
        "https://www.ozon.ru/product/a-111\n"
        "https://www.ozon.ru/product/b-222\n",
        encoding="utf-8",
    )

    part_contents = {
        0: [
            {"review_id": "r1", "text": "a"},
            {"review_id": "r2", "text": "a"},
        ],
        1: [
            {"review_id": "r2", "text": "dup"},
            {"review_id": "r3", "text": "b"},
        ],
    }

    spawned: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))
        # Find our part index via --output argument value.
        out_idx = cmd.index("--output") + 1
        part_path = Path(cmd[out_idx])
        part_path.write_text(
            "".join(
                json.dumps(r, ensure_ascii=False) + "\n"
                for r in part_contents[part_idx_of(part_path)]
            ),
            encoding="utf-8",
        )
        return _FakeProc()

    def part_idx_of(part: Path) -> int:
        return int(part.stem.rsplit("p", 1)[-1])

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_exec,
    )

    args = _products_args(
        products_file=str(products),
        output=str(tmp_path / "out.jsonl"),
    )
    total = await run_products_parallel(args)

    assert total == 3  # r1, r2, r3
    out = tmp_path / "out.jsonl"
    ids = [
        json.loads(line)["review_id"]
        for line in open(out, encoding="utf-8")
    ]
    assert ids == ["r1", "r2", "r3"]
    # по одному дочернему процессу на товар
    assert len(spawned) == 2
    # part-файлы удалены после мерджа
    assert not (tmp_path / "out.p000.jsonl").exists()
    assert not (tmp_path / "out.p001.jsonl").exists()


@pytest.mark.asyncio
async def test_run_products_parallel_respects_concurrency(
    tmp_path, monkeypatch,
):
    """Не более --products-sessions детей одновременно."""
    products = tmp_path / "products.txt"
    products.write_text(
        "\n".join(
            f"https://www.ozon.ru/product/x-{i}" for i in range(5)
        ),
        encoding="utf-8",
    )

    running = 0
    peak = 0

    class _FakeProc:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*cmd, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        out_idx = cmd.index("--output") + 1
        Path(cmd[out_idx]).write_text(
            '{"review_id": "r"}\n', encoding="utf-8",
        )
        running -= 1
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_exec,
    )
    args = _products_args(
        products_file=str(products),
        products_sessions=2,
        output=str(tmp_path / "out.jsonl"),
    )
    await run_products_parallel(args)
    assert peak <= 2


@pytest.mark.asyncio
async def test_run_products_parallel_requires_ozon():
    args = _products_args(marketplace="wildberries")
    with pytest.raises(SystemExit):
        await run_products_parallel(args)


def test_cli_products_file_and_url_are_mutually_exclusive():
    from marketplace_maps_parser.__main__ import parse_args

    with pytest.raises(SystemExit):
        parse_args([
            "--marketplace", "ozon",
            "--url", "https://www.ozon.ru/product/a",
            "--products-file", "products.txt",
        ])


def test_cli_requires_url_or_products_file():
    from marketplace_maps_parser.__main__ import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--marketplace", "ozon"])


def test_cli_products_file_requires_ozon():
    from marketplace_maps_parser.__main__ import parse_args

    with pytest.raises(SystemExit):
        parse_args([
            "--marketplace", "wildberries",
            "--products-file", "products.txt",
        ])


def test_cli_products_file_ok_without_url():
    from marketplace_maps_parser.__main__ import parse_args

    args = parse_args([
        "--marketplace", "ozon",
        "--products-file", "products.txt",
    ])
    assert args.products_file == "products.txt"
    assert args.url is None
    assert args.products_sessions == 3
