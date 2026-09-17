# src/infrastructure/transports/browser.py
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any


def _import_invisible_playwright():
    """Lazy import; see transports/browser_json.py for rationale.
    Wrapped with GPU-safe software-rendering prefs (see
    transports/gpu_safety.py)."""
    from invisible_playwright.async_api import InvisiblePlaywright

    from infrastructure.transports.gpu_safety import make_gpu_safe
    return make_gpu_safe(InvisiblePlaywright)


class BrowserJsonTransport:
    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 5_000,
        debug_dir: str = "debug_ozon",
        proxy: dict[str, str] | None = None,
        seed: int | None = None,
        pin: dict[str, Any] | None = None,
        humanize: bool = True,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.seed = seed
        self.pin = pin
        self.humanize = humanize

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_page_json(
        self,
        page_url: str,
        *,
        response_marker: str | None = None,
        wait_for: str | None = None,
    ) -> dict[str, Any]:
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        captured: list[dict[str, Any]] = []
        response_log: list[dict[str, Any]] = []

        async with _import_invisible_playwright()(
            proxy=self.proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()

            async def on_response(response) -> None:
                request = response.request
                resource_type = request.resource_type

                record = {
                    "status": response.status,
                    "url": response.url,
                    "resource_type": resource_type,
                    "content_type": response.headers.get(
                        "content-type",
                        "",
                    ),
                }

                # Сохраняем только интересующие сетевые типы.
                if resource_type in {"xhr", "fetch"}:
                    response_log.append(record)

                content_type = record["content_type"].lower()

                is_json = (
                    "json" in content_type
                    or "graphql" in content_type
                )

                if not is_json:
                    return

                try:
                    body = await response.json()
                except Exception:
                    return

                if not isinstance(body, (dict, list)):
                    return

                captured.append(
                    {
                        **record,
                        "body": body,
                    }
                )

            page.on("response", on_response)

            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            await asyncio.sleep(self.settle_ms / 1000)

            if wait_for:
                try:
                    await page.wait_for_selector(
                        wait_for,
                        timeout=15_000,
                    )
                except Exception:
                    pass

            # Прокрутка помогает активировать lazy-loaded widgets.
            await page.mouse.wheel(0, 1800)
            await asyncio.sleep(2)

            await page.mouse.wheel(0, 2200)
            await asyncio.sleep(3)

            await self._save_debug(
                page=page,
                page_url=page_url,
                captured=captured,
                response_log=response_log,
            )

            candidates = self._select_candidates(
                captured=captured,
                response_marker=response_marker,
            )

            if not candidates:
                raise RuntimeError(
                    "Не найден JSON-ответ с отзывами. "
                    f"Текущий URL: {page.url}. "
                    f"XHR/fetch ответов: {len(response_log)}. "
                    f"Диагностика сохранена в {self.debug_dir}"
                )

            return candidates[-1]["body"]

    async def _save_debug(
        self,
        *,
        page,
        page_url: str,
        captured: list[dict[str, Any]],
        response_log: list[dict[str, Any]],
    ) -> None:
        safe_name = re.sub(
            r"[^a-zA-Z0-9_-]+",
            "_",
            page_url,
        )[:80]

        (self.debug_dir / f"{safe_name}.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        await page.screenshot(
            path=str(self.debug_dir / f"{safe_name}.png"),
            full_page=True,
        )

        (self.debug_dir / f"{safe_name}_responses.json").write_text(
            json.dumps(
                response_log,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (self.debug_dir / f"{safe_name}_json.json").write_text(
            json.dumps(
                captured,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _select_candidates(
        *,
        captured: list[dict[str, Any]],
        response_marker: str | None,
    ) -> list[dict[str, Any]]:
        if response_marker:
            marked = [
                item
                for item in captured
                if response_marker.lower()
                in item["url"].lower()
            ]

            if marked:
                return marked

        # Более широкий fallback: ищем признаки отзывов
        # не только в URL, но и в теле ответа.
        candidates: list[dict[str, Any]] = []

        for item in captured:
            text = json.dumps(
                item["body"],
                ensure_ascii=False,
            ).lower()

            markers = (
                "review",
                "reviews",
                "отзыв",
                "rating",
                "оценк",
            )

            if any(marker in text for marker in markers):
                candidates.append(item)

        return candidates