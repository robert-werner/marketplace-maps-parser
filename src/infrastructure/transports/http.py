# src/infrastructure/transports/http.py
from __future__ import annotations

from typing import Any

import httpx


class HttpStatusError(RuntimeError):
    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code}: {url}")
        self.status_code = status_code
        self.url = url


class HttpJsonTransport:
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers=headers or {},
        )

    async def __aenter__(self) -> HttpJsonTransport:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.get(
            url,
            params=params,
            headers=headers,
        )

        if response.status_code >= 400:
            raise HttpStatusError(
                status_code=response.status_code,
                url=str(response.url),
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(
                f"Ожидался JSON, получен {content_type}: {response.url}"
            )

        payload = response.json()

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Корень ответа не является dict: {response.url}"
            )

        return payload

    async def get_page_json(
        self,
        page_url: str,
        *,
        response_marker: str,
        wait_for: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "HttpJsonTransport не умеет загружать browser-only страницы"
        )