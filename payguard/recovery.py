"""Startup recovery (design §3 Sugg. 5, PRD F10).

Spawned by the FastMCP lifespan as a background task after a 2 s sleep, so it never blocks the
initialize handshake. Touches only rows older than STALE_S; every write is compare-and-set.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from payguard.models import SIGNED_IN_FLIGHT, UNSIGNED_IN_FLIGHT, PaymentStatus, history_entry
from payguard.purchase import CALL_BUDGET_S, reconcile_and_finish

log = logging.getLogger(__name__)
P = PaymentStatus
STALE_S = 60.0


async def run_startup_recovery(deps: Any) -> None:
    repo = deps.repo
    stuck = repo.list_stuck(STALE_S)
    if not stuck:
        log.info("startup recovery: nothing to do")
        return
    for row in stuck:
        st = row.payment_status
        if st in UNSIGNED_IN_FLIGHT:
            if repo.update_status(row.purchase_id, st, P.ABANDONED, history_entry("ABANDONED", by="startup_recovery", was=str(st))):
                log.info("startup recovery: %s %s -> ABANDONED (nothing was signed)", row.purchase_id, st)
        elif st in SIGNED_IN_FLIGHT:
            if repo.update_status(row.purchase_id, st, P.SETTLEMENT_UNKNOWN,
                                  history_entry("SETTLEMENT_UNKNOWN", by="startup_recovery", reason=f"found {st} after restart")):
                log.info("startup recovery: %s %s -> SETTLEMENT_UNKNOWN; reconciling", row.purchase_id, st)
                try:
                    await reconcile_and_finish(deps, repo.get_purchase(row.purchase_id), time.monotonic() + CALL_BUDGET_S)
                except Exception:  # noqa: BLE001
                    log.exception("startup recovery: reconcile failed for %s", row.purchase_id)
