"""Exceptions for browser_json transport."""
from __future__ import annotations


class CloudflareChallengeError(RuntimeError):
    """Raised when Ozon returns HTTP 403 with a Cloudflare
    ``challenge.html`` body instead of the expected JSON.

    Triggers a much longer backoff than a generic transient error:
    Cloudflare expects clients to wait 10+ seconds between
    challenge-failed retries, otherwise it keeps returning the
    challenge indefinitely. Subclasses RuntimeError so existing
    retry_on filters that include RuntimeError still catch it.
    """

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        # Truncate the body so log lines stay readable — the
        # challenge body is a long base64-ish blob.
        preview = body[:200] + "..." if len(body) > 200 else body
        super().__init__(
            f"Cloudflare challenge (HTTP {status}) on {url}: "
            f"body={preview}"
        )


