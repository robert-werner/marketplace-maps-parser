"""Durable run state and atomic checkpoint helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RUN_STATUSES = ("complete", "partial", "failed")


def status_path(output: str | Path) -> Path:
    """Return the sidecar path used for JSONL run state."""
    return Path(f"{output}.status.json")


def infer_status(
    *,
    error: str | None,
    interrupted: bool = False,
    record_count: int,
    expected_count: int | None = None,
    stopped_by_limit: bool = False,
) -> str:
    """Classify a run without claiming completeness we cannot prove."""
    if error or interrupted:
        return "partial" if record_count else "failed"
    if stopped_by_limit:
        return "partial"
    if expected_count is not None and record_count < expected_count:
        return "partial"
    return "complete"


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Write JSON so a killed process cannot leave a half-document."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp",
    )
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_status(
    output: str | Path,
    *,
    status: str,
    collected: int,
    total_records: int,
    error: str | None = None,
    interrupted: bool = False,
    expected_count: int | None = None,
    **extra: Any,
) -> None:
    """Persist a small status/checkpoint sidecar for JSONL runs."""
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown run status: {status!r}")
    atomic_write_json(
        status_path(output),
        {
            "status": status,
            "error": error,
            "interrupted": interrupted,
            "collected": collected,
            "total_records": total_records,
            "expected_count": expected_count,
            **extra,
        },
    )
