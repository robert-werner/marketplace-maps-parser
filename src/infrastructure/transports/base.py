"""Shared helpers and constants for the Ozon transports.

The Ozon browser JSON transport iterates the review stream and parses
the same payloads. This module holds the pieces they would otherwise
copy-paste:

- the Ozon base URL and internal API endpoint constants;
- review-widget URL construction and ``nextPage`` extraction;
- best-effort review id extraction and review-node identification;
- the Cloudflare challenge-body heuristic.

Transports mix in :class:`OzonTransportMixin` so ``self._helper()``
call sites and class-level test calls keep working.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

OZON_BASE_URL = "https://www.ozon.ru"
OZON_API_ENDPOINT = f"{OZON_BASE_URL}/api/entrypoint-api.bx/page/json/v2"


class OzonTransportMixin:
    """Mixin with static helpers shared by the Ozon transports."""

    @staticmethod
    def _build_initial_path(
        *,
        product_path: str,
        page_number: int,
    ) -> str:
        """Build the first review-widget path for a product."""
        return f"{product_path}/reviews?page={page_number}"

    @staticmethod
    def _absolute_url(path: str) -> str:
        """Turn an Ozon-internal path into an absolute URL."""
        if path.startswith(("http://", "https://")):
            return path

        return f"{OZON_BASE_URL}{path}"

    @staticmethod
    def _build_api_url(internal_path: str) -> str:
        """Full internal-API URL for a review-widget path."""
        return f"{OZON_API_ENDPOINT}?{urlencode({'url': internal_path})}"

    @staticmethod
    def extract_next_path(
        payload: dict[str, Any],
    ) -> str | None:
        """Extract the next page path from an Ozon API payload.

        ``nextPage`` may be a plain path string or a dict with one of
        ``url`` / ``href`` / ``path`` keys.
        """
        next_page = payload.get("nextPage")

        if isinstance(next_page, str):
            return next_page or None

        if isinstance(next_page, dict):
            for key in ("url", "href", "path"):
                value = next_page.get(key)

                if isinstance(value, str) and value:
                    return value

        return None

    @staticmethod
    def _review_node_id(node: dict[str, Any]) -> str | None:
        """Best-effort extraction of a stable id from a review node."""
        from infrastructure.marketplaces.ozon_payload import extract_review_id

        review_id = extract_review_id(node)
        if review_id:
            return review_id
        for key in (
            "reviewId",
            "review_id",
            "reviewUuid",
            "review_uuid",
            "uuid",
            "id",
        ):
            value = node.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _extract_review_nodes_from_payload(
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Pull every dict that looks like a review out of a raw
        Ozon pagination payload.

        Reuses the payload parser's matcher without constructing the full
        Review objects a second time just to count pagination results.
        """
        from infrastructure.marketplaces.ozon_payload import (
            iter_ozon_review_nodes,
        )

        return list(iter_ozon_review_nodes(payload))

    def _notify_pacer_block(self, page: Any = None) -> None:
        """Feed an antibot/challenge event to the run's pacer.

        Each API tab owns its pacer, so a failure in one sort cannot
        overwrite another sort's delay. The legacy ``_pacer`` attribute
        remains supported for callers that use a single iterator.
        """
        pacer = getattr(self, "_pacer", None)
        if page is not None:
            pacer = getattr(self, "_page_pacers", {}).get(page, pacer)
        if pacer is not None:
            pacer.record_block()

    @staticmethod
    def _is_cloudflare_challenge(body: str) -> bool:
        """Heuristic for detecting a Cloudflare challenge response body.

        Two known shapes:

        1. JSON envelope (older API-level challenge):
           ``{"incidentId": "fab_chlg_...", "challengeURL": "..."}``

        2. HTML "Browser Challenge" page (Cloudflare Under-Attack
           interstitial):
           Contains ``Пожалуйста, включите JavaScript`` /
           ``enable JavaScript to continue`` /
           ``We need to make sure that you are not a robot`` /
           an ``ID: fab_chlg_...`` line.
        """
        if not body:
            return False
        if body.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(body)
            except ValueError:
                pass
            else:
                # Review text may mention JavaScript/challenges; only
                # top-level challenge metadata is a JSON interstitial.
                return isinstance(payload, dict) and bool(
                    {"challengeurl", "incidentid"}
                    & {str(key).lower() for key in payload}
                )
        body_lower = body.lower()
        return (
            "challengeurl" in body_lower
            or "incidentid" in body_lower
            or "challenge.html" in body_lower
            # HTML challenge page markers
            or "fab_chlg_" in body_lower
            or "enable javascript" in body_lower
            or "включите javascript" in body_lower
            or "we need to make sure that you are not a robot"
            in body_lower
            or "нам нужно убедиться, что вы не робот"
            in body_lower
        )
