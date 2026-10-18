"""Opt-in truncation for the demo sellers (imports nothing from the guard package).

SELLER_TRUNCATE_PATHS is a comma-separated list of route prefixes ("/convert", "/report"). When a 2xx response on a
matching path carries the PAYMENT-RESPONSE header (so the payment is already settled, same idea as sellers/stall.py),
the body is cut to RATIO of its bytes and sent with a Content-Length that matches the cut bytes. The buyer therefore
receives a syntactically short PDF/JSON that looks complete at the transport level, which is what a seller bug or a
dropped upstream stream produces.

A "declared" variant (cut body under the ORIGINAL Content-Length, so the buyer's transport check fires too) is not
offered: uvicorn's h11 and httptools backends both abort the response with "Response content shorter than
Content-Length" and the buyer sees a closed connection (httpx RemoteProtocolError), never a short body. PayGuard's
transport check is exercised in the test suite through its fake seller instead.

With the env var unset nothing is installed and every route behaves as before.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from starlette.datastructures import MutableHeaders

log = logging.getLogger("sellers.truncate")
RATIO = 0.6
PAYMENT_RESPONSE_HEADER = "payment-response"  # x402 v2 settlement receipt header (lower-cased for lookup)

ASGIApp = Callable[..., Awaitable[None]]


def truncate_paths(value: str | None = None) -> tuple[str, ...]:
    """Parse SELLER_TRUNCATE_PATHS (or the given string): trimmed, non-empty, must start with '/'. Others are ignored."""
    raw = os.environ.get("SELLER_TRUNCATE_PATHS", "") if value is None else value
    out: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        if p.startswith("/") and p not in out:
            out.append(p)
        elif p:
            log.warning("SELLER_TRUNCATE_PATHS: ignoring %r (route prefixes start with '/')", p)
    return tuple(out)


def cut_length(size: int, ratio: float = RATIO) -> int:
    return max(1, int(size * ratio)) if size > 0 else 0


class TruncateMiddleware:
    """Pure ASGI middleware: buffers the response of a matching route and forwards a cut copy after settlement."""

    def __init__(self, app: ASGIApp, paths: tuple[str, ...], ratio: float = RATIO) -> None:
        self.app = app
        self.paths = tuple(paths)
        self.ratio = ratio

    def matches(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.paths)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not self.matches(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []

        async def _send(message: dict[str, Any]) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                headers = MutableHeaders(raw=start["headers"])
                if 200 <= start["status"] < 300 and PAYMENT_RESPONSE_HEADER in headers:
                    cut = body[: cut_length(len(body), self.ratio)]
                    log.info("%s settled; truncating the body to %d of %d bytes", scope.get("path"), len(cut), len(body))
                    headers["content-length"] = str(len(cut))
                    body = cut
                await send(start)
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, _send)


def install_truncation(app: Any, paths: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Add the middleware (outermost, so it sees the x402 middleware's final response) when any path is configured."""
    paths = truncate_paths() if paths is None else tuple(paths)
    if paths:
        app.add_middleware(TruncateMiddleware, paths=paths)
        log.warning("SELLER_TRUNCATE_PATHS=%s: settled responses on these routes are cut to %d%%", ",".join(paths), int(RATIO * 100))
    return paths
