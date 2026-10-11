"""Tests for the unified output format."""
from __future__ import annotations

from datetime import datetime

from shared.entities import ProductRef, Review
from shared.unified_format import (
    UNIFIED_REVIEW_FIELDS,
    build_unified_document,
    build_unified_review,
    compose_review_text,
    normalize_unified_rating,
    unified_review_id,
    video_count_from_raw,
    video_durations_from_raw,
    video_len_from_raw,
)


def _review(**overrides: object) -> Review:
    defaults: dict[str, object] = {
        "review_id": "r-1",
        "product": ProductRef(
            marketplace="yandex_maps",
            source_url="https://yandex.ru/maps/org/x/1",
            product_id="1",
        ),
        "rating": 5,
        "text": " Отличный сервис ",
        "pros": None,
        "cons": None,
        "author": "Иван",
        "created_at": datetime(2021, 12, 25, 12, 35, 46),
        "photos": ["https://a.example/1.jpg"],
        "raw": {
            "reviewId": "r-1",
            "videos": [
                {"duration": 30.5},
                "https://example.com/video2.mp4",
                {"durationSeconds": 45},
            ],
        },
    }
    defaults.update(overrides)
    return Review(**defaults)  # type: ignore[arg-type]


def test_unified_review_field_set_and_order() -> None:
    record = build_unified_review(_review(), product_title="Почта")
    assert tuple(record) == UNIFIED_REVIEW_FIELDS
    assert record["source_url"] == (
        "https://yandex.ru/maps/org/x/1"
    )
    assert record["platform"] == "yandex_maps"
    assert record["product_title"] == "Почта"
    assert record["text"] == "Отличный сервис"
    assert record["text_len"] == len("Отличный сервис")
    assert record["rating"] == 5
    assert record["review_date"] == "2021-12-25"
    assert record["photos"] == 1
    assert record["video_count"] == 3  # mixed: 2 dicts + 1 URL
    assert record["video_durations"] == [30.5, 45.0]
    assert record["video_len"] == 30.5


def test_unified_review_no_videos() -> None:
    review = _review(raw={"reviewId": "r-1", "videos": []})
    record = build_unified_review(review)
    assert record["video_count"] == 0
    assert record["video_durations"] == []
    assert record["video_len"] is None


def test_text_merges_pros_and_cons() -> None:
    review = _review(
        text="Комментарий",
        pros="Качество",
        cons="Цена",
    )
    text = compose_review_text(review)
    assert text == (
        "Достоинства: Качество\n"
        "Недостатки: Цена\n"
        "Комментарий"
    )
    record = build_unified_review(review)
    assert record["text_len"] == len(text.strip())


def test_text_len_counts_stripped_text() -> None:
    review = _review(text="  пробелы по краям  ")
    record = build_unified_review(review)
    assert record["text"] == "пробелы по краям"
    assert record["text_len"] == len("пробелы по краям")


def test_empty_review_text() -> None:
    record = build_unified_review(_review(text=None))
    assert record["text"] is None
    assert record["text_len"] == 0


def test_video_len_from_raw() -> None:
    assert video_len_from_raw(
        {"videos": [{"duration": 12.5}]},
    ) == 12.5
    assert video_len_from_raw(
        {"media": [{"durationSeconds": 40}]},
    ) == 40.0
    assert video_len_from_raw({"videos": []}) is None
    assert video_len_from_raw({}) is None
    assert video_len_from_raw("не словарь") is None


def test_video_count_from_raw() -> None:
    assert video_count_from_raw({
        "videos": [
            {"duration": 30.5},
            "https://example.com/v.mp4",
            {"durationSeconds": 45},
        ],
    }) == 3
    # Mixed string URLs and object payloads (WB browser transport)
    assert video_count_from_raw({
        "videos": [
            "https://example.com/a.mp4",
            "https://example.com/b.mp4",
        ],
    }) == 2
    assert video_count_from_raw({"videos": []}) == 0
    assert video_count_from_raw({}) == 0
    assert video_count_from_raw("не словарь") == 0
    # Multiple containers
    assert video_count_from_raw({
        "videos": ["a"],
        "media": [{"duration": 10}],
    }) == 2


def test_video_durations_from_raw() -> None:
    assert video_durations_from_raw({
        "videos": [
            {"duration": 30.5},
            "https://example.com/v.mp4",
            {"durationSeconds": 45},
        ],
    }) == [30.5, 45.0]  # URLs are skipped; only dicts with durations
    assert video_durations_from_raw({
        "videos": [
            "https://example.com/a.mp4",
            "https://example.com/b.mp4",
        ],
    }) == []  # pure URLs — no durations
    assert video_durations_from_raw({"videos": []}) == []
    assert video_durations_from_raw({}) == []
    assert video_durations_from_raw("не словарь") == []
    # Multiple containers
    assert video_durations_from_raw({
        "videos": [{"duration": 10}],
        "media": [{"length": 20}],
    }) == [10.0, 20.0]


def test_rating_normalisation() -> None:
    assert normalize_unified_rating(4.6) == 5
    assert normalize_unified_rating("3") == 3
    assert normalize_unified_rating(None) is None
    assert normalize_unified_rating(True) is None
    assert normalize_unified_rating(0) is None
    assert normalize_unified_rating(9) is None


def test_document_ok_and_error() -> None:
    doc = build_unified_document([], error=None)
    assert doc["diagnostics"]["status"] == "complete"
    assert doc["diagnostics"]["error"] is None

    doc = build_unified_document(
        [],
        error="YandexCaptchaError: капча",
        total_count=86,
    )
    assert doc["reviews"] == []
    assert doc["diagnostics"]["status"] == "failed"
    assert doc["diagnostics"]["error"] == (
        "YandexCaptchaError: капча"
    )
    assert doc["diagnostics"]["total_count"] == 86


def test_unified_review_id_recovery() -> None:
    assert unified_review_id(
        {"raw": {"reviewId": "mGuY…"}},
    ) == "mGuY…"
    assert unified_review_id({"raw": {"id": 143420181}}) == (
        "143420181"
    )
    assert unified_review_id({"raw": {"uuid": "u-1"}}) == "u-1"
    assert unified_review_id({"raw": {}}) is None
    assert unified_review_id({}) is None
