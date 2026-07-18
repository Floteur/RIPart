"""Download an avatar image and return it as a ``data:`` URI."""

from __future__ import annotations

import base64

import httpx

from .http import HttpClient

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def fetch_avatar(
    http: HttpClient,
    url: str,
    *,
    referer: str,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> str:
    """GET ``url`` through ``http``'s pooled client, return a ``data:`` URI (or '').

    Providers build the concrete image URL (Saucepan's ``/cdn/<id>/card``,
    clank's absolute avatar URL) and hand it here; the download + size cap +
    base64 encoding are identical across platforms.
    """
    if not url:
        return ""
    try:
        response = http.client().get(
            url,
            headers={
                "User-Agent": http.user_agent,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Referer": referer,
            },
        )
        if response.is_error:
            return ""
        content = response.content
        if len(content) > max_bytes:
            return ""
        content_type = response.headers.get("content-type") or "image/jpeg"
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except httpx.HTTPError:
        return ""
