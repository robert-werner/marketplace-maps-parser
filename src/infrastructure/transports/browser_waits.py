"""Push-based DOM readiness helpers for Invisible Playwright pages."""
from __future__ import annotations

from typing import Any

_CARD_SIGNATURE_JS = """
selector => Array.from(document.querySelectorAll(selector),
    el => (el.getAttribute('data-review-uuid') || el.id || '')
        + ':' + (el.textContent || '')).join('\\u001f')
"""

_CARD_CHANGE_JS = """
({selector, previous, timeoutMs, quietMs}) => new Promise(resolve => {
    let observer;
    let deadline;
    let quiet;
    let finished = false;
    const signature = () => Array.from(
        document.querySelectorAll(selector),
        el => (el.getAttribute('data-review-uuid') || el.id || '')
            + ':' + (el.textContent || ''),
    ).join('\\u001f');
    const finish = changed => {
        if (finished) return;
        finished = true;
        observer.disconnect();
        clearTimeout(deadline);
        clearTimeout(quiet);
        resolve({changed});
    };
    const check = () => {
        clearTimeout(quiet);
        const current = signature();
        if (current && current !== previous) {
            quiet = setTimeout(() => {
                if (signature() === current) finish(true);
                else check();
            }, quietMs);
        }
    };
    observer = new MutationObserver(check);
    observer.observe(document.documentElement, {
        childList: true, subtree: true, characterData: true,
        attributes: true, attributeFilter: ['class', 'style', 'fill'],
    });
    deadline = setTimeout(() => finish(false), timeoutMs);
    check();
})
"""


async def card_signature(page: Any, selector: str) -> str:
    result = await page.evaluate(_CARD_SIGNATURE_JS, selector)
    return str(result or "")


async def wait_for_card_change(
    page: Any, *, selector: str, previous: str, timeout_ms: int,
) -> bool:
    if timeout_ms <= 0:
        return False
    result = await page.evaluate(_CARD_CHANGE_JS, {
        "selector": selector, "previous": previous,
        "timeoutMs": timeout_ms, "quietMs": min(200, timeout_ms),
    })
    if not isinstance(result, dict) or "changed" not in result:
        raise TypeError("Unexpected card-change result")
    return bool(result["changed"])
