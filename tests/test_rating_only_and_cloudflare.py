"""Tests for rating-only review collection and Cloudflare challenge
detection.

Two production concerns addressed in this commit:

1. Many Ozon shoppers leave a star rating without writing any text.
   Previously the DOM scroll parser hard-coded rating=None, and the
   JSON parser didn't read rating from scroll cards. Now both paths
   extract rating, and ``is_review_node`` accepts rating-only
   reviews (they still have ``rating`` / ``score`` / ``stars`` keys).

2. Cloudflare returns HTTP 403 with a ``challenge.html`` body when
   the browser session is flagged. The previous retry helper applied
   a 1.5s base delay — too short for Cloudflare, which expects 10s+
   waits. The new ``CloudflareChallengeError`` triggers a longer
   backoff (3s base, 30s cap) so retries don't burn attempts on
   immediate re-fires.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from domain.entities import ProductRef
from infrastructure.marketplaces.ozon import (
    OzonAdapter,
    is_review_node,
    map_ozon_review_node,
)
from infrastructure.transports.browser_json import (
    BrowserJsonTransport,
    CloudflareChallengeError,
)


PRODUCT = ProductRef(
    marketplace="ozon",
    source_url="https://www.ozon.ru/product/foo-12345",
    product_id="12345",
)


# ---------------------------------------------------------------------------
# Rating-only review acceptance
# ---------------------------------------------------------------------------


def test_rating_only_review_is_accepted_by_is_review_node():
    """A review with rating but no text should still be recognized
    as a review node.
    """
    node = {
        "reviewId": "r1",
        "rating": 5,
        # No "text" key — rating-only review
    }

    assert is_review_node(
        node=node,
        review_id="r1",
        text=None,
        rating=5,
    )


def test_rating_only_review_is_mapped():
    """map_ozon_review_node accepts a rating-only node and produces
    a Review with rating=5, text=None."""
    node = {
        "reviewId": "r-rating-only",
        "rating": 5,
    }
    review = map_ozon_review_node(node=node, product=PRODUCT)
    assert review is not None
    assert review.review_id == "r-rating-only"
    assert review.rating == 5
    assert review.text is None


def test_review_with_text_and_rating_still_works():
    """Standard review: text + rating. No regression."""
    node = {
        "reviewId": "r-full",
        "rating": 4,
        "text": "Good product",
    }
    review = map_ozon_review_node(node=node, product=PRODUCT)
    assert review is not None
    assert review.review_id == "r-full"
    assert review.rating == 4
    assert review.text == "Good product"


def test_review_with_no_id_no_rating_is_rejected():
    """A dict with no review_id and no rating marker is not a review."""
    node = {"foo": "bar", "baz": 42}
    assert not is_review_node(
        node=node,
        review_id=None,
        text=None,
        rating=None,
    )


def test_extract_reviews_from_ozon_payload_includes_rating_only():
    """End-to-end: rating-only reviews in a payload are extracted,
    not silently dropped.
    """
    from infrastructure.marketplaces.ozon import (
        extract_reviews_from_ozon_payload,
    )

    payload = {
        "reviews": [
            {"reviewId": "r1", "rating": 5, "text": "Good"},
            {"reviewId": "r2", "rating": 4},  # rating-only
            {"reviewId": "r3", "rating": 1, "text": "Bad"},
            {"reviewId": "r4", "rating": 5},  # rating-only
        ]
    }
    reviews = extract_reviews_from_ozon_payload(payload, PRODUCT)

    # All 4 should be extracted
    assert len(reviews) == 4
    ids = {r.review_id for r in reviews}
    assert ids == {"r1", "r2", "r3", "r4"}


# ---------------------------------------------------------------------------
# parse_ozon_dom_card — rating extraction
# ---------------------------------------------------------------------------


def test_parse_ozon_dom_card_reads_rating_from_card():
    """parse_ozon_dom_card should use the ``rating`` field from the
    card dict (set by BrowserDomTransport._read_review_card), not
    hard-code rating=None.
    """
    adapter = OzonAdapter(browser_transport=None)  # type: ignore
    card = {
        "uuid": "dom-1",
        # DOM transport extracts author cleanly and puts it in the
        # card dict; the raw text still contains all the lines.
        "author": "Author Name",
        "text": "Author Name\n2024-01-01\nGood product",
        "rating": 4,
        "published_at": "1704067200",
    }
    review = adapter.parse_ozon_dom_card(card=card, product=PRODUCT)

    assert review.review_id == "dom-1"
    assert review.rating == 4
    assert review.author == "Author Name"
    # When the DOM transport does NOT pre-extract review_text, the
    # adapter uses the raw text. The fallback heuristic for author
    # (lines[1]) is bypassed because ``author`` is provided.
    # The text returned is the raw text since no ``review_text``
    # field was provided.
    assert review.text == "Author Name\n2024-01-01\nGood product"


def test_parse_ozon_dom_card_rating_only_review():
    """A rating-only DOM card (no review text, just rating) should
    still produce a Review with rating set and text=None.
    """
    adapter = OzonAdapter(browser_transport=None)  # type: ignore
    card = {
        "uuid": "dom-rating-only",
        "text": "",  # No text — only the rating
        "rating": 5,
        "published_at": "1704067200",
    }
    review = adapter.parse_ozon_dom_card(card=card, product=PRODUCT)

    assert review.review_id == "dom-rating-only"
    assert review.rating == 5
    # text was empty string → normalize_text → None
    assert review.text is None


def test_parse_ozon_dom_card_falls_back_when_rating_missing():
    """If the card has no rating (older transport), we still produce
    a Review with rating=None — no regression.
    """
    adapter = OzonAdapter(browser_transport=None)  # type: ignore
    card = {
        "uuid": "dom-no-rating",
        "text": "Author\n2024-01-01\nSome text",
    }
    review = adapter.parse_ozon_dom_card(card=card, product=PRODUCT)

    assert review.review_id == "dom-no-rating"
    assert review.rating is None
    assert review.text == "Author\n2024-01-01\nSome text"


def test_parse_ozon_dom_card_prefers_review_text_field():
    """When the DOM transport pre-extracts a cleaner ``review_text``
    field, the adapter should prefer it over the raw multi-line
    ``text`` field.
    """
    adapter = OzonAdapter(browser_transport=None)  # type: ignore
    card = {
        "uuid": "dom-clean",
        "text": "Initials\nAuthor Name\n2024-01-01\nReview text here\n"
                "Вам помог этот отзыв?\nДа 5 Нет 1",
        "review_text": "Review text here",  # pre-extracted by DOM transport
        "rating": 5,
        "published_at": "1704067200",
    }
    review = adapter.parse_ozon_dom_card(card=card, product=PRODUCT)

    assert review.rating == 5
    # Should use the cleaned review_text, not the noisy raw text
    assert review.text == "Review text here"


# ---------------------------------------------------------------------------
# Cloudflare challenge detection
# ---------------------------------------------------------------------------


def test_is_cloudflare_challenge_detects_challenge_url():
    """A body containing 'challengeURL' (Ozon's actual challenge
    JSON shape) is detected as a Cloudflare challenge."""
    body = (
        '{"incidentId": "fab_chlg_2026...", '
        '"challengeURL": "https://www.ozon.ru/challenge.html?..."}'
    )
    assert BrowserJsonTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_detects_incident_id():
    """A body containing 'incidentId' alone is also detected."""
    body = '{"incidentId": "fab_chlg_2026..."}'
    assert BrowserJsonTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_detects_challenge_html():
    """A body containing 'challenge.html' is also detected."""
    body = "<html><body>challenge.html page</body></html>"
    assert BrowserJsonTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_rejects_normal_response():
    """A normal Ozon JSON response is NOT a challenge."""
    body = '{"reviews": [{"reviewId": "r1", "rating": 5}]}'
    assert not BrowserJsonTransport._is_cloudflare_challenge(body)


def test_is_cloudflare_challenge_rejects_empty_body():
    assert not BrowserJsonTransport._is_cloudflare_challenge("")


def test_is_cloudflare_challenge_rejects_none_body():
    assert not BrowserJsonTransport._is_cloudflare_challenge(None)  # type: ignore


def test_cloudflare_challenge_error_carries_context():
    """CloudflareChallengeError stores status, url, body for
    debugging / logging."""
    err = CloudflareChallengeError(
        status=403,
        url="https://www.ozon.ru/api/entrypoint-api.bx/...",
        body='{"incidentId": "fab_chlg_...", "challengeURL": "..."}',
    )
    assert err.status == 403
    assert "challengeURL" in err.url or "entrypoint" in err.url
    assert "challengeURL" in err.body
    # Truncation works for long bodies
    long_body = "x" * 500
    long_err = CloudflareChallengeError(
        status=403, url="https://x", body=long_body,
    )
    assert "..." in str(long_err)
    assert len(str(long_err)) < 600  # truncated, not the full 500 chars


# ---------------------------------------------------------------------------
# _fetch_json_with_retry retries CloudflareChallengeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_with_retry_retries_on_cloudflare_challenge(monkeypatch):
    """When the inner fetch raises CloudflareChallengeError on
    attempts 1, 2 and returns successfully on attempt 3, the retry
    wrapper should retry and return the successful payload.
    """
    transport = BrowserJsonTransport()

    call_count = {"n": 0}

    async def fake_inner_fetch(*, page, internal_path):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise CloudflareChallengeError(
                status=403,
                url="https://www.ozon.ru/api/...",
                body='{"incidentId": "fab_chlg_..."}',
            )
        return {"ok": True, "page": internal_path}

    monkeypatch.setattr(
        transport,
        "_fetch_json_inside_page",
        fake_inner_fetch,
    )

    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    result = await transport._fetch_json_with_retry(
        page=None,  # type: ignore
        internal_path="/p",
        attempts=5,
        label="test",
    )

    assert result == {"ok": True, "page": "/p"}
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_cloudflare_challenge_is_runtime_error_subclass():
    """CloudflareChallengeError must be a RuntimeError subclass so
    existing retry_on=(RuntimeError,) filters still catch it
    (e.g. when used as a transport for the OzonAdapter fallback path).
    """
    err = CloudflareChallengeError(
        status=403, url="x", body="y",
    )
    assert isinstance(err, RuntimeError)
