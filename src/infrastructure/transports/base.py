"""Shared helpers and constants for the Ozon transports.

The Ozon transport implementations (``browser_json``, ``curl_cffi``,
``hybrid``, ``public_page``) iterate the same review stream and parse
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

        Reuses ``walk_json`` semantics from the adapter without
        duplicating its fuzzy matcher — this is intentionally a thin
        pass-through that just surfaces raw candidate nodes.
        """
        # Local import to avoid a hard dep cycle with the adapter module.
        from domain.entities import ProductRef
        from infrastructure.marketplaces.ozon import (
            extract_reviews_from_ozon_payload,
        )

        # extract_reviews_from_ozon_payload needs a ProductRef for
        # building Review objects, but we only need the raw node
        # identification logic. Pass a minimal placeholder.
        placeholder = ProductRef(
            marketplace="ozon",
            source_url="",
            product_id="_placeholder",
        )
        reviews = extract_reviews_from_ozon_payload(
            payload, placeholder,
        )
        # ``raw`` field on each Review is the original node dict
        return [r.raw for r in reviews if isinstance(r.raw, dict)]

    def _notify_pacer_block(self) -> None:
        """Feed an antibot/challenge event to the run's pacer.

        ``iter_all_ozon_reviews`` implementations store an
        :class:`~shared.pacing.AdaptivePacer` on ``self._pacer``
        (created from ``page_delay_seconds``); antibot detection
        paths call this hook so the inter-page delay backs off
        after challenges. No-op when no pacer is active.
        """
        pacer = getattr(self, "_pacer", None)
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
