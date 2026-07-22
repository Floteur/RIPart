"""Shared pooled HTTP client with retry + wire tracing.

One :class:`HttpClient` per provider (each has its own ``base_url`` and auth), so
this is a small framework, not a global singleton: a single pooled ``httpx``
client would have to pin one base URL, which two providers can't share. Each
provider instantiates an ``HttpClient`` and builds its own request semantics
(REST vs tRPC) on top of :meth:`HttpClient.send`, which centralises connection
pooling, network/5xx retry with ``Retry-After``, and the ``-vv``/``-vvv`` wire
tracing both providers print.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from .errors import RipError

# Transient statuses worth retrying: rate-limit + the standard 5xx family.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFF = 0.75  # seconds; exponential (0.75s, 1.5s, ...)


class HttpClient:
    """A base-URL-scoped pooled ``httpx.Client`` with retry and tracing.

    ``httpx.Client`` is thread-safe, so the multi-account leak bench can drive
    one instance from several worker threads concurrently (each carries its own
    auth via per-request headers, not client state).
    """

    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        trace_name: str,
        error_label: str,
        error_cls: type[RipError] = RipError,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url
        self.user_agent = user_agent
        self.trace_name = trace_name  # e.g. "saucepan-http"
        self.error_label = error_label  # e.g. "Saucepan" (network-error message)
        self.error_cls = error_cls
        self.timeout = timeout
        self.trace_level = 0
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def client(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(
                        base_url=self.base_url,
                        timeout=self.timeout,
                        # requests followed redirects by default; httpx does not.
                        follow_redirects=True,
                        limits=httpx.Limits(
                            max_connections=20, max_keepalive_connections=10
                        ),
                    )
        return self._client

    # -- tracing ----------------------------------------------------------- #

    def set_trace_level(self, level: int) -> None:
        """Enable wire tracing at ``level`` (0 off, 2 call summaries, 3 payloads)."""
        self.trace_level = max(0, int(level or 0))

    def trace_preview(self, value: Any, limit: int = 800) -> str:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = " ".join(text.split())
        return (
            text if len(text) <= limit else f"{text[:limit]}… ({len(text)} chars total)"
        )

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return _RETRY_BACKOFF * (2**attempt)

    # -- request ----------------------------------------------------------- #

    def send(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        attempts: int = 1,
        retry_5xx: bool = False,
        timeout: int | None = None,
        trace_label: str | None = None,
    ) -> httpx.Response:
        """One HTTP round trip with retry, returning the raw ``httpx.Response``.

        Never raises on an HTTP *status* (callers inspect ``response``); only
        raises ``error_cls`` when every attempt fails with a network error.
        Retries network errors and — when ``retry_5xx`` — transient 429/5xx,
        honouring a numeric ``Retry-After``. ``trace_label`` overrides the path
        shown in traces (e.g. a tRPC procedure name).
        """
        label = trace_label or path
        client = self.client()
        for attempt in range(max(1, attempts)):
            last = attempt == attempts - 1
            started = time.monotonic()
            try:
                response = client.request(
                    method, path, headers=headers, json=json_body, timeout=timeout
                )
            except httpx.HTTPError as exc:
                if self.trace_level >= 2:
                    print(
                        f"[{self.trace_name}] {method} {label} -> network error: {exc}",
                        flush=True,
                    )
                if last:
                    raise self.error_cls(
                        f"network error talking to {self.error_label}: {exc}"
                    ) from exc
                time.sleep(_RETRY_BACKOFF * (2**attempt))
                continue
            elapsed_ms = (time.monotonic() - started) * 1000
            if self.trace_level >= 2:
                retried = f" (attempt {attempt + 1})" if attempt else ""
                print(
                    f"[{self.trace_name}] {method} {label} -> {response.status_code} "
                    f"({elapsed_ms:.0f}ms){retried}",
                    flush=True,
                )
            if self.trace_level >= 3:
                if json_body is not None:
                    print(
                        f"[{self.trace_name}]   body: {self.trace_preview(json_body)}",
                        flush=True,
                    )
                print(
                    f"[{self.trace_name}]   resp: {self.trace_preview(response.text)}",
                    flush=True,
                )
            if retry_5xx and response.status_code in RETRYABLE_STATUS and not last:
                time.sleep(self._retry_delay(response, attempt))
                continue
            return response
        raise self.error_cls("request failed")  # pragma: no cover - loop returns/raises
