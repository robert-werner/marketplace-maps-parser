# src/domain/ports.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from typing_extensions import AsyncIterator


class JsonTransport(ABC):
    @abstractmethod
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class BrowserTransport(ABC):
    @abstractmethod
    async def get_page_json(
        self,
        url: str,
        *,
        wait_for: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

class ReviewTransport(ABC):
    @abstractmethod
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_page_json(
        self,
        page_url: str,
        *,
        response_marker: str,
        wait_for: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

class ReviewPageSource(ABC):
    @abstractmethod
    def iter_pages(
        self,
        page_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        raise NotImplementedError