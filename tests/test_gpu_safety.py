"""Tests for transports/gpu_safety.make_gpu_safe."""
from __future__ import annotations

from typing import Any

from infrastructure.transports.gpu_safety import (
    GPU_SAFE_PREFS,
    gpu_safety_enabled,
    make_gpu_safe,
)


class DummyBrowser:
    """Captures launch kwargs — no real browser involved."""

    def __init__(
        self,
        seed: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.seed = seed
        self.kwargs = kwargs


def test_returns_subclass_with_merged_prefs():
    safe_cls = make_gpu_safe(DummyBrowser)

    assert issubclass(safe_cls, DummyBrowser)
    assert "GpuSafe" in safe_cls.__name__

    instance = safe_cls(seed=1, humanize=True)
    merged = instance.kwargs["extra_prefs"]

    for key, value in GPU_SAFE_PREFS.items():
        assert merged[key] == value


def test_caller_prefs_win_per_key():
    safe_cls = make_gpu_safe(DummyBrowser)

    instance = safe_cls(
        extra_prefs={"gfx.canvas.accelerated": True},
    )
    merged = instance.kwargs["extra_prefs"]

    assert merged["gfx.canvas.accelerated"] is True
    # Untouched keys still get the safety defaults.
    assert merged["webgl.force-enabled"] is True


def test_webgl_fingerprint_prefs_not_overridden():
    """The overlay must not touch the stealth WebGL surface."""
    for key in GPU_SAFE_PREFS:
        assert not key.startswith("zoom.stealth")

    # And must NOT disable WebGL or the GPU process (invisible_core
    # documents that webgl.out-of-process=False crashes content).
    assert "webgl.out-of-process" not in GPU_SAFE_PREFS
    assert "webgl.disabled" not in GPU_SAFE_PREFS


def test_env_off_returns_stock_class(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_GPU_SAFETY", "off")

    assert make_gpu_safe(DummyBrowser) is DummyBrowser


def test_gpu_safety_enabled_env_variants(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_GPU_SAFETY", "1")
    assert gpu_safety_enabled() is True

    monkeypatch.setenv("MARKETPLACE_GPU_SAFETY", "no")
    assert gpu_safety_enabled() is False

    monkeypatch.setenv("MARKETPLACE_GPU_SAFETY", "FALSE")
    assert gpu_safety_enabled() is False

    monkeypatch.delenv("MARKETPLACE_GPU_SAFETY", raising=False)
    assert gpu_safety_enabled() is True
