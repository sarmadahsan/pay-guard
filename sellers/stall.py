"""Scenario B after-settle stall for /research-slow (design §4, N1). Imports nothing from the guard package.

The hook runs after the facilitator has settled on-chain and the receipt was awaited. Sleeping here
only delays the HTTP response; the transfer is already final. Gated on the HTTPRequestContext path,
so /report, /report-premium and /convert are unaffected. Never raises: a raising after-settle hook
turns the settle into a 402 for every route.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("sellers.stall")
STALL_PATH = "/research-slow"


def make_stall_hook(stall_s: float, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
    async def stall_after_settle(ctx: Any) -> None:
        try:
            tc = getattr(ctx, "transport_context", None)
            req = getattr(tc, "request", None) if tc is not None else None
            path = getattr(req, "path", None)
            if path == STALL_PATH:
                log.info("%s settled; stalling the response for %ss", STALL_PATH, stall_s)
                await sleep(stall_s)
        except Exception:  # noqa: BLE001
            log.exception("stall hook error (ignored so the settle still succeeds)")

    return stall_after_settle
