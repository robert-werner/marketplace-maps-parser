from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from invisible_playwright.async_api import InvisiblePlaywright


class BrowserJsonTransport:
    """Получает JSON Ozon в одной browser-сессии.

    Сначала открывается страница отзывов, затем внутренний endpoint
    вызывается из этой же страницы через window.fetch(). Следующая
    страница берётся из поля nextPage ответа Ozon.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 2_000,
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

    async def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        self.debug_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with InvisiblePlaywright(
            proxy=self.proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()
            current_path = self._build_initial_path(
                product_path=product_path,
                page_number=start_page,
            )
            seen_paths: set[str] = set()
            processed_pages = 0

            while current_path:
                if (
                    max_pages is not None
                    and processed_pages >= max_pages
                ):
                    return

                if current_path in seen_paths:
                    return

                seen_paths.add(current_path)

                reviews_url = self._absolute_url(current_path)

                await page.goto(
                    reviews_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )

                if self.settle_ms > 0:
                    await page.wait_for_timeout(
                        self.settle_ms,
                    )

                payload = await self._fetch_json_inside_page(
                    page=page,
                    internal_path=current_path,
                )

                processed_pages += 1

                await self._save_debug(
                    page=page,
                    payload=payload,
                    page_number=processed_pages,
                )

                next_path = self.extract_next_path(payload)

                yield processed_pages, payload

                current_path = next_path

    async def get_ozon_reviews_json(
        self,
        product_path: str,
        *,
        page_number: int = 1,
    ) -> dict[str, Any]:
        async for current_page, payload in (
            self.iter_ozon_reviews_json(
                product_path=product_path,
                start_page=page_number,
                max_pages=1,
            )
        ):
            if current_page == 1:
                return payload

        raise RuntimeError(
            f"Не удалось получить страницу Ozon {page_number}"
        )

    async def _fetch_json_inside_page(
        self,
        *,
        page,
        internal_path: str,
    ) -> dict[str, Any]:
        endpoint = (
            "https://www.ozon.ru"
            "/api/entrypoint-api.bx/page/json/v2"
        )

        endpoint_url = (
            f"{endpoint}?"
            f"{urlencode({'url': internal_path})}"
        )

        result = await page.evaluate(
            """
            async (endpointUrl) => {
                const response = await fetch(endpointUrl, {
                    method: "GET",
                    credentials: "include",
                    headers: {
                        "Accept": "application/json"
                    }
                });

                return {
                    status: response.status,
                    url: response.url,
                    contentType:
                        response.headers.get("content-type") || "",
                    body: await response.text()
                };
            }
            """,
            endpoint_url,
        )

        status = result["status"]
        response_url = result["url"]
        content_type = result["contentType"].lower()
        body = result["body"]

        if status < 200 or status >= 300:
            raise RuntimeError(
                "Ozon browser fetch завершился ошибкой: "
                f"HTTP {status}; url={response_url}; "
                f"body={body[:1000]}"
            )

        if "json" not in content_type:
            raise RuntimeError(
                "Ozon browser fetch вернул не JSON: "
                f"content-type={content_type}; "
                f"body={body[:500]}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Не удалось декодировать JSON Ozon: {body[:500]}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Корень JSON Ozon не является dict"
            )

        return payload

    async def _save_debug(
        self,
        *,
        page,
        payload: dict[str, Any],
        page_number: int,
    ) -> None:
        page_dir = self.debug_dir / f"page_{page_number}"
        page_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        (page_dir / "response.json").write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (page_dir / "page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        await page.screenshot(
            path=str(page_dir / "page.png"),
            full_page=True,
        )

    @staticmethod
    def _absolute_url(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path

        return f"https://www.ozon.ru{path}"

    @staticmethod
    def _build_initial_path(
        *,
        product_path: str,
        page_number: int,
    ) -> str:
        query = urlencode({"page": page_number})
        return f"{product_path}/reviews?{query}"

    @staticmethod
    def extract_next_path(
        payload: dict[str, Any],
    ) -> str | None:
        next_page = payload.get("nextPage")

        if isinstance(next_page, str):
            return next_page or None

        if isinstance(next_page, dict):
            for key in ("url", "href", "path"):
                value = next_page.get(key)

                if isinstance(value, str) and value:
                    return value

        return None
