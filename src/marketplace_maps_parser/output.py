"""Append-only checkpoints; unified JSON is streamed atomically at finish."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from domain.entities import Review
from marketplace_maps_parser.run_state import atomic_write_json, status_path
from shared.unified_format import build_unified_review, unified_review_id


def review_to_record(review: Review) -> dict[str, Any]:
    """Legacy JSONL fields plus identity/media needed for reliable resume."""
    return {
        "review_id": review.review_id,
        "source_url": review.product.source_url,
        "product_id": review.product.product_id,
        "marketplace": review.product.marketplace,
        "rating": review.rating,
        "text": review.text,
        "author": review.author,
        "created_at": review.created_at,
        "pros": review.pros,
        "cons": review.cons,
        "photos": review.photos,
        "seller_answer": review.seller_answer,
        "raw": review.raw,
    }


def record_key(record: dict[str, Any]) -> str:
    """Scope source IDs to a platform/product; keep ID-less reviews too."""
    rid = record.get("review_id") or unified_review_id(record)
    scope = (
        record.get("marketplace") or record.get("platform") or "",
        record.get("product_id") or record.get("source_url") or "",
    )
    if rid is None:
        rid = hashlib.sha256(json.dumps(
            {k: record.get(k) for k in (
                "author", "created_at", "review_date", "rating", "text",
                "pros", "cons",
            )}, sort_keys=True, ensure_ascii=False, default=str,
        ).encode()).hexdigest()
    return json.dumps([*scope, str(rid)], ensure_ascii=False)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Strict reader: malformed data is never silently discarded."""
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: invalid JSONL") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{number}: expected an object")
            yield record


def repair_interrupted_tail(path: Path) -> None:
    """Drop only a torn last write, never malformed middle records."""
    with path.open("rb+") as file:
        end = file.seek(0, os.SEEK_END)
        if not end:
            return
        file.seek(end - 1)
        if file.read(1) == b"\n":
            return
        position = end
        tail = b""
        while position:
            size = min(position, 8192)
            position -= size
            file.seek(position)
            tail = file.read(size) + tail
            if b"\n" in tail:
                break
        line = tail.rsplit(b"\n", 1)[-1]
        try:
            json.loads(line)
        except (ValueError, UnicodeDecodeError):
            file.truncate(end - len(line))
        else:
            file.seek(0, os.SEEK_END)
            file.write(b"\n")


def write_document(
    path: Path,
    records: Iterator[dict[str, Any]],
    diagnostics: dict[str, Any],
    *,
    product_title: str | None = None,
) -> None:
    """Stream an array, with one-record memory, into an atomic replacement."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("w", encoding="utf-8") as file:
            file.write('{"reviews":[\n')
            separator = ""
            for record in records:
                if product_title is not None:
                    record["product_title"] = product_title
                file.write(separator)
                json.dump(record, file, ensure_ascii=False, default=str)
                separator = ",\n"
            file.write('\n],"diagnostics":')
            json.dump(diagnostics, file, ensure_ascii=False, default=str)
            file.write("}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ReviewWriter:
    """JSONL writes directly; JSON uses a recoverable JSONL journal.

    Only dedup keys grow with the run. Checkpoints fsync newly appended data,
    rather than repeatedly serializing every previous review (quadratic I/O).
    """

    def __init__(
        self, output: Path, *, output_format: str, resume: bool,
        source_url: str,
    ) -> None:
        self.output = output
        self.format = output_format
        self.source_url = source_url
        self.journal = Path(f"{output}.checkpoint.jsonl")
        self.data_path = self.journal if output_format == "json" else output
        self.seen: set[str] = set()
        self.total = 0
        self.checkpoints = 0
        self.file: TextIO
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = status_path(output)
        if resume and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("source_url") not in (None, source_url):
                raise ValueError("--resume: output belongs to another URL")
            if metadata.get("format") not in (None, output_format):
                raise ValueError("--resume: output format changed")

        if resume and self.data_path.exists():
            repair_interrupted_tail(self.data_path)
            # Validate BEFORE opening for append; never replace broken data.
            for record in iter_jsonl(self.data_path):
                self.seen.add(record_key(record))
                self.total += 1
        elif resume and output.exists() and output_format == "json":
            document = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or not isinstance(
                document.get("reviews"), list,
            ):
                raise ValueError("--resume: expected a unified JSON document")
            with self.journal.open("w", encoding="utf-8") as file:
                for record in document["reviews"]:
                    if not isinstance(record, dict):
                        raise ValueError("--resume: invalid review record")
                    source = record.get("source_url")
                    if source and source != source_url:
                        raise ValueError(
                            "--resume: review belongs to another URL"
                        )
                    file.write(json.dumps(
                        record, ensure_ascii=False, default=str,
                    ) + "\n")
                    self.seen.add(record_key(record))
                    self.total += 1
        self.file = self.data_path.open(
            "a" if resume else "w", encoding="utf-8", buffering=262144,
        )
        self.initial_total = self.total

    def add(self, review: Review) -> bool:
        record = (
            build_unified_review(review) if self.format == "json"
            else review_to_record(review)
        )
        key = record_key(record)
        if key in self.seen:
            return False
        self.file.write(json.dumps(
            record, ensure_ascii=False, default=str,
        ) + "\n")
        self.seen.add(key)
        self.total += 1
        return True

    def checkpoint(self, diagnostics: dict[str, Any]) -> None:
        self.file.flush()
        os.fsync(self.file.fileno())
        self.checkpoints += 1
        atomic_write_json(status_path(self.output), {
            **diagnostics,
            "source_url": self.source_url,
            "format": self.format,
            "total_records": self.total,
            "checkpoints": self.checkpoints,
        })

    def finish(
        self, diagnostics: dict[str, Any], *, product_title: str | None,
    ) -> None:
        self.checkpoint(diagnostics)
        if self.format == "json":
            write_document(
                self.output, iter_jsonl(self.journal), {
                    **diagnostics, "total_records": self.total,
                    "checkpoints": self.checkpoints,
                }, product_title=product_title,
            )
        self.file.close()
        if self.format == "json":
            # Final output is safely replaced first. On a failed replace,
            # the journal remains available for recovery.
            self.journal.unlink(missing_ok=True)

    def close(self) -> None:
        self.file.close()
