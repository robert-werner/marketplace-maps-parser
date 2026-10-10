"""Legacy Ozon aggregate rating export (not individual source reviews)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _scan_output_ratings(
    output: Path,
    product_id: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """Scan the output JSONL: total rows per star and existing
    synthetic rating-only rows per star (ids prefixed
    ``<product_id>-ro-<star>-``).
    """
    per_star: dict[str, int] = {}
    synth: dict[str, int] = {}
    prefix = f"{product_id}-ro-"
    try:
        with output.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rating = record.get("rating")
                if rating is None or isinstance(rating, bool):
                    continue
                try:
                    star = str(int(rating))
                except (TypeError, ValueError):
                    continue
                per_star[star] = per_star.get(star, 0) + 1
                rid = str(record.get("review_id") or "")
                if rid.startswith(f"{prefix}{star}-"):
                    synth[star] = synth.get(star, 0) + 1
    except OSError:
        pass
    return per_star, synth


def _finalize_rating_summary(
    *,
    output: Path,
    summary: dict[str, Any],
    include_rating_only: bool,
    marketplace: str,
) -> int:
    """Write ``<output>.summary.json``; with ``include_rating_only``
    also append synthetic rows for the rating-only remainder.

    Returns the number of synthetic rows appended this run.
    """
    histogram = summary.get("histogram") or {}
    product_id = str(summary.get("product_id") or "")
    if not histogram or not product_id:
        return 0

    per_star, synth = _scan_output_ratings(output, product_id)

    remainder = {
        star: max(0, int(count) - per_star.get(star, 0))
        for star, count in histogram.items()
    }

    added = 0
    if include_rating_only and any(remainder.values()):
        with output.open("a", encoding="utf-8") as file:
            for star in sorted(remainder, reverse=True):
                need = remainder[star]
                if need <= 0:
                    continue
                start = synth.get(star, 0)
                for n in range(start + 1, start + need + 1):
                    record = {
                        "review_id": (
                            f"{product_id}-ro-{star}-{n:05d}"
                        ),
                        "product_id": product_id,
                        "marketplace": marketplace,
                        "rating": int(star),
                        "text": None,
                        "author": None,
                        "created_at": None,
                        "pros": None,
                        "cons": None,
                        "seller_answer": None,
                        "raw": {
                            "synthetic": True,
                            "source": (
                                "webReviewProductScore histogram"
                            ),
                            "note": (
                                "Оценка без отзыва: Ozon не отдаёт "
                                "такие записи по отдельности"
                            ),
                        },
                    }
                    file.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
                    added += 1

    summary_record = {
        "product_id": product_id,
        "product_url": summary.get("product_url"),
        "average_score": summary.get("average_score"),
        "site_ratings_total": summary.get("reviews_count"),
        "site_histogram": histogram,
        "rows_per_star_in_file": per_star,
        "rating_only_per_star": remainder,
        "synthetic_rows_appended_this_run": added,
        "synthetic_rows_total_in_file": sum(synth.values()) + added,
    }
    summary_path = Path(str(output) + ".summary.json")
    summary_path.write_text(
        json.dumps(
            summary_record,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    histogram_preview = " ".join(
        f"{star}*={count}"
        for star, count in sorted(
            histogram.items(),
            reverse=True,
        )
    )
    print(
        f"Ozon: гистограмма оценок: {histogram_preview}; "
        f"оценок без текста (нельзя собрать индивидуально): "
        f"{sum(remainder.values())}"
        + (
            f"; добавлено синтетических строк: {added}"
            if added
            else ""
        )
        + f"; сводка: {summary_path.name}"
    )
    return added


