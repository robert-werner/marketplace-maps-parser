"""Atomic, format-aware merging of child outputs."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from marketplace_maps_parser.output import (
    iter_jsonl,
    record_key,
    write_document,
)
from marketplace_maps_parser.run_state import atomic_write_json, status_path


def read_part(path: Path, output_format: str) -> Iterator[dict[str, Any]]:
    if output_format == "jsonl":
        yield from iter_jsonl(path)
        return
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(
        document.get("reviews"), list,
    ):
        raise ValueError(f"{path}: expected a unified review document")
    for record in document["reviews"]:
        if not isinstance(record, dict):
            raise ValueError(f"{path}: invalid review")
        yield record


def merge_parts(
    part_paths: list[Path], out_path: Path, *, output_format: str,
    exit_codes: list[int | None], max_reviews: int | None = None,
    resume: bool = False, interrupted: bool = False,
    page_ranges: bool = False,
) -> tuple[int, str]:
    """Preserve every available row, propagate failure and commit atomically.

    Parts are deliberately NOT deleted here. The supervisor removes only
    fully successful parts after the final result is durably committed.
    """
    seen: set[str] = set()
    errors: list[str] = []
    children: list[dict[str, Any]] = []
    count = 0
    limited = False
    diagnostics: dict[str, Any] = {}

    def records() -> Iterator[dict[str, Any]]:
        nonlocal count, limited
        if resume and out_path.exists():
            # Invalid pre-existing output must abort before replacement.
            for record in read_part(out_path, output_format):
                key = record_key(record)
                if key not in seen:
                    seen.add(key)
                    count += 1
                    yield record
        for index, path in enumerate(part_paths):
            code = exit_codes[index]
            state: dict[str, Any] = {}
            if status_path(path).exists():
                try:
                    state = json.loads(status_path(path).read_text())
                except (ValueError, OSError):
                    errors.append(f"{path.name}: unreadable run status")
            elif output_format == "json" and path.exists():
                try:
                    document = json.loads(path.read_text())
                    state = document.get("diagnostics") or {}
                except (ValueError, OSError, AttributeError):
                    errors.append(f"{path.name}: unreadable JSON output")
            children.append({"part": path.name, "exit_code": code, **state})
            # A bounded page chunk is not a complete product by itself.
            # Do not treat that intentional scope restriction as a failure;
            # the parent compares its merged count with the source total.
            expected_chunk = (
                page_ranges and code == 3 and not state.get("error")
                and state.get("stop_reason") == "exhausted"
                and not state.get("incomplete_reason")
            )
            if ((code != 0 or state.get("status") in ("partial", "failed"))
                    and not expected_chunk):
                reason = state.get("error") or state.get("status") or code
                errors.append(f"{path.name}: {reason}")
            if not path.exists():
                errors.append(f"{path.name}: missing child output")
                continue
            try:
                for record in read_part(path, output_format):
                    key = record_key(record)
                    if key in seen:
                        continue
                    if max_reviews is not None and count >= max_reviews:
                        limited = True
                        continue
                    seen.add(key)
                    count += 1
                    yield record
            except (OSError, ValueError) as exc:
                # Already-valid rows survive a torn/invalid child output,
                # but neither that child nor the merged run is "complete".
                errors.append(f"{path.name}: {exc}")
        totals = [
            c["expected_count"] for c in children
            if isinstance(c.get("expected_count"), int)
        ]
        expected = max(totals) if page_ranges and totals else None
        incomplete = interrupted or limited or bool(errors)
        if page_ranges and (expected is None or count < expected):
            incomplete = True
        status = (
            "partial" if incomplete and (count or limited or not errors)
            else "failed" if incomplete else "complete"
        )
        diagnostics.update(
            status=status, error="; ".join(errors) or None,
            interrupted=interrupted, collected=count, total_records=count,
            expected_count=expected,
            completeness_verified=(
                not incomplete and (
                    count >= expected if expected is not None
                    else all(c.get("completeness_verified") for c in children)
                )
            ),
            children=children,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        # diagnostics is populated as the generator drains, before the footer.
        write_document(out_path, records(), diagnostics)
    else:
        temp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as file:
                for record in records():
                    file.write(json.dumps(
                        record, ensure_ascii=False, default=str,
                    ) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, out_path)
        finally:
            temp.unlink(missing_ok=True)
    atomic_write_json(status_path(out_path), diagnostics)
    return count, str(diagnostics["status"])
