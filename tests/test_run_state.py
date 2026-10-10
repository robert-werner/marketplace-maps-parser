from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.entities import ProductRef, Review
from marketplace_maps_parser.merging import merge_parts
from marketplace_maps_parser.run_state import status_path
from marketplace_maps_parser.runner import run_collection

URL = "https://www.ozon.ru/product/demo-123/"


def _review(review_id: str) -> Review:
    return Review(
        review_id=review_id,
        product=ProductRef("ozon", URL, "123"),
        rating=5,
        text=f"review {review_id}",
        raw={"reviewId": review_id},
    )


def _args(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        output=str(path),
        format="json",
        resume=False,
        url=URL,
        marketplace="ozon",
        max_reviews=None,
        checkpoint_interval=1,
        checkpoint_seconds=60.0,
        start_page=1,
        max_pages=None,
    )


@pytest.mark.asyncio
async def test_failed_collection_is_saved_as_partial(tmp_path: Path) -> None:
    output = tmp_path / "reviews.json"
    args = _args(output)
    adapter = SimpleNamespace(
        last_product_title="Demo",
        last_review_count=3,
    )

    async def stream():
        yield _review("r1")
        yield _review("r2")
        raise RuntimeError("blocked")

    count = await run_collection(
        args,
        adapter=adapter,
        make_iterator=stream,
        extra_diagnostics=lambda: {
            "total_count": adapter.last_review_count,
        },
    )

    assert count == 2
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["diagnostics"]["status"] == "partial"
    assert document["diagnostics"]["total_records"] == 2
    assert status_path(output).exists()


@pytest.mark.asyncio
async def test_cancellation_keeps_checkpoint_and_marks_partial(
    tmp_path: Path,
) -> None:
    output = tmp_path / "reviews.json"
    args = _args(output)
    adapter = SimpleNamespace(last_product_title=None)
    ready = asyncio.Event()

    async def stream():
        yield _review("r1")
        ready.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(
        run_collection(args, adapter=adapter, make_iterator=stream),
    )
    await ready.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["diagnostics"]["status"] == "partial"
    assert document["diagnostics"]["interrupted"] is True
    assert document["reviews"][0]["raw"]["reviewId"] == "r1"


def test_parallel_json_parts_are_merged_without_data_loss(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.json"
    first.write_text(json.dumps({
        "reviews": [{
            "source_url": URL,
            "platform": "ozon",
            "raw": {"reviewId": "r1"},
            "text": "a",
        }],
        "diagnostics": {
            "status": "complete",
            "expected_count": 1,
            "completeness_verified": True,
        },
    }), encoding="utf-8")
    second.write_text(json.dumps({
        "reviews": [{
            "source_url": URL,
            "platform": "ozon",
            "raw": {"reviewId": "r2"},
            "text": "b",
        }],
        "diagnostics": {"status": "partial", "error": "child failed"},
    }), encoding="utf-8")

    count, status = merge_parts(
        [first, second],
        output,
        output_format="json",
        exit_codes=[0, 3],
    )

    assert count == 2
    assert status == "partial"
    document = json.loads(output.read_text(encoding="utf-8"))
    assert [r["raw"]["reviewId"] for r in document["reviews"]] == [
        "r1", "r2",
    ]
    assert document["diagnostics"]["status"] == "partial"
