# src/infrastructure/transports/gpu_safety.py
"""GPU-safe browser launching for invisible-playwright sessions.

Scrapers run many long-lived Firefox sessions on commodity hosts and
cloud VMs where the GPU driver stack is the single most flaky
dependency: a TDR reset, a hung compositor or a missing GPU takes the
whole run down. ``make_gpu_safe`` wraps the ``InvisiblePlaywright``
class so every session launches with software rendering:

- WebRender stays enabled but falls back to its software renderer
  (no D3D11/GL compositor dependency);
- the GPU process is allowed to start in software mode;
- 2D canvas acceleration is off (the content process then never
  touches a hardware GPU at all);
- WebGL keeps working through ANGLE's WARP (CPU) path, so the
  ``webgl`` fingerprint surface invisible-playwright spoofs is still
  present — only the hardware behind it changes.

The stealth fingerprint is untouched: ``InvisiblePlaywright`` applies
its profile prefs first and ``extra_prefs`` LAST (verified in
``invisible_core.prefs._apply_caller_overlay``), so our overrides
cannot leak around the spoofed GPU class, and the spoofed
RENDERER/WEBGL parameters still report the persona's hardware.

Usage (the pattern already used by every browser transport)::

    from infrastructure.transports.gpu_safety import make_gpu_safe

    browser_cls = make_gpu_safe(InvisiblePlaywright)
    async with browser_cls(proxy=..., seed=..., humanize=True) as ip:
        ...
"""
from __future__ import annotations

import os
from typing import Any

# Firefox prefs that pin the rendering stack to software. Values and
# rationale follow invisible_core's own virtual-desktop workarounds
# (its ``_WIN_VIRT_DESKtop_WORKAROUNDS`` ships the same pair for
# headless-on-Windows), generalised to every host: a scraper does not
# need a hardware compositor, and a crashed GPU process costs a whole
# page fetch.
GPU_SAFE_PREFS: dict[str, Any] = {
    # WebRender: enabled, but its backend falls back to software when
    # no hardware compositor is reachable (instead of retrying
    # ``ConnectToCompositor`` forever, which is how a GPU-less host
    # hangs the browser).
    "gfx.webrender.software": True,
    # Let the GPU process itself come up in software mode; without
    # this a failed D3D11 init crashes it and takes the tab down.
    "layers.gpu-process.enabled": True,
    "layers.gpu-process.max_restarts": 5,
    # 2D canvas: the content process renders on the CPU. The canvas
    # fingerprint surface is spoofed by the stealth layer anyway.
    "gfx.canvas.accelerated": False,
    # ANGLE through WARP keeps WebGL contexts alive on hosts without
    # a usable GPU — same trick invisible_core uses on its virtual
    # desktops, applied unconditionally here.
    "webgl.angle.force-warp": True,
    # Never hard-crash on WebGL: report the (spoofed) parameters
    # instead of tearing down the content process.
    "webgl.force-enabled": True,
}

# One-time env switch for opt-out: set MARKETPLACE_GPU_SAFETY to
# "off"/"0"/"false"/"no" to keep the stock launch behaviour on a
# host where hardware rendering genuinely works and is wanted.


def gpu_safety_enabled() -> bool:
    """Whether GPU-safe prefs should be injected (default: yes)."""
    raw = os.environ.get("MARKETPLACE_GPU_SAFETY", "on")
    return raw.strip().lower() not in {"off", "0", "false", "no"}


def make_gpu_safe[T](browser_cls: type[T]) -> type[T]:
    """Return a subclass of ``browser_cls`` that injects
    software-rendering prefs into every launch.

    The wrapper merges :data:`GPU_SAFE_PREFS` into whatever
    ``extra_prefs`` the caller passes (caller wins per-key so a
    transport can still fine-tune a single pref), and is a no-op
    subclass when :func:`gpu_safety_enabled` is False.
    """
    if not gpu_safety_enabled():
        return browser_cls

    def _gpu_safe_init(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        extra = dict(kwargs.pop("extra_prefs", None) or {})
        for key, value in GPU_SAFE_PREFS.items():
            # Caller-provided prefs win over the safety defaults.
            extra.setdefault(key, value)
        kwargs["extra_prefs"] = extra
        browser_cls.__init__(self, *args, **kwargs)

    gpu_safe_cls = type(
        browser_cls.__name__ + "GpuSafe",
        (browser_cls,),
        {"__init__": _gpu_safe_init},
    )
    return gpu_safe_cls


__all__ = ["GPU_SAFE_PREFS", "gpu_safety_enabled", "make_gpu_safe"]
