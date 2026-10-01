"""Generic HTTP transport for interop protocol drivers.

Every external request carries an identity-disclosing user-agent, a bounded
timeout, and a strict response-size cap so no single server can starve or
overflow the runtime.  Transport is injectable so local tests never touch the
network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

DEFAULT_USER_AGENT = os.environ.get(
    "IDENTITYOS_USER_AGENT",
    "identityos/0.5.0 (+https://github.com/lacebx/IdentityOS)",
)

DEFAULT_TIMEOUT = 30.0

# Responses larger than this are truncated before parsing; a server advertising
# an unbounded payload must never be allowed to exhaust memory.
MAX_RESPONSE_BYTES = 2_000_000

MAX_REQUEST_BYTES = 1_000_000


class HttpError(Exception):
    """Non-2xx HTTP response, retaining status and (scrubbed) body hint."""


class HttpTooLargeError(HttpError):
    """Response exceeded MAX_RESPONSE_BYTES."""


@dataclass
class HttpResponse:
    status: int
    content_type: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)


Transport = Callable[[Mapping[str, Any], float], "tuple[int, str, str, dict[str, str]]"]


def default_transport(request: Mapping[str, Any], timeout: float) -> tuple[int, str, str, dict[str, str]]:
    """Send an HTTP request and return (status, content-type, body-text, headers).

    ``request`` keys: url, method, headers, body (bytes or None).  HTTPError is
    surfaced as a normal (code, headers, body) response so callers can unify
    error handling.
    """
    req = urllib.request.Request(
        request["url"],
        data=request.get("body"),
        headers=dict(request.get("headers") or {}),
        method=request.get("method", "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(MAX_RESPONSE_BYTES + 1)
            return (
                res.status,
                res.headers.get("content-type", ""),
                raw[:MAX_RESPONSE_BYTES].decode("utf-8", "replace"),
                dict(res.headers),
            )
    except urllib.error.HTTPError as err:
        raw = err.read(MAX_RESPONSE_BYTES + 1)
        return (
            err.code,
            err.headers.get("content-type", "") if err.headers else "",
            raw[:MAX_RESPONSE_BYTES].decode("utf-8", "replace"),
            dict(err.headers) if err.headers else {},
        )


def parse_body(text: str, content_type: str = ""):
    """Parse an HTTP body: JSON when possible, otherwise last SSE ``data:`` frame."""
    if not text:
        return text
    if "text/event-stream" in content_type or _looks_like_sse(text):
        data_lines = [
            line[5:].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            text = data_lines[-1]
    if not text:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _looks_like_sse(text: str) -> bool:
    return any(
        line.strip().startswith("event:") or line.strip().startswith("data:")
        for line in text.splitlines()
    )


class HttpClient:
    """Small, transport-injectable HTTP client used by protocol drivers."""

    def __init__(
        self,
        *,
        base_headers: Optional[Mapping[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[Transport] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        self.timeout = timeout
        self._transport = transport or default_transport
        self._base_headers: dict[str, str] = dict(base_headers or {})
        self._base_headers.setdefault("user-agent", user_agent or DEFAULT_USER_AGENT)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Any = None,
    ) -> HttpResponse:
        merged_headers = dict(self._base_headers)
        merged_headers.update(headers or {})
        raw: Optional[bytes] = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                raw = bytes(body)
            elif isinstance(body, str):
                raw = body.encode("utf-8")
            else:
                raw = json.dumps(body).encode("utf-8")
            if raw and len(raw) > MAX_REQUEST_BYTES:
                raise HttpError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
            merged_headers.setdefault("content-type", "application/json")
            merged_headers.setdefault("accept", "application/json")
        status, content_type, text, headers = self._transport(
            {"url": url, "method": method, "headers": merged_headers, "body": raw},
            self.timeout,
        )
        if len(text) > MAX_RESPONSE_BYTES:
            raise HttpTooLargeError(
                f"{method} {url}: response exceeded {MAX_RESPONSE_BYTES} bytes"
            )
        return HttpResponse(status=status, content_type=content_type, text=text, headers=headers)

    def get(self, url: str, *, headers: Optional[Mapping[str, str]] = None) -> HttpResponse:
        return self.request("GET", url, headers=headers)

    def post(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Any = None,
    ) -> HttpResponse:
        return self.request("POST", url, headers=headers, body=body)

    def delete(self, url: str, *, headers: Optional[Mapping[str, str]] = None) -> HttpResponse:
        return self.request("DELETE", url, headers=headers)