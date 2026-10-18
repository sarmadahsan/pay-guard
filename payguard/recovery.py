"""Startup recovery (design §3, PRD F10). Spawned from the FastMCP lifespan after a 2 s sleep.

M2 STUB: does nothing. M3 (Task 3.2) implements the 60 s staleness rule with compare-and-set writes.
"""
from __future__ import annotations

from typing import Any


async def run_startup_recovery(deps: Any) -> None:
    return None
