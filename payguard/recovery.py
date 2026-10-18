"""Startup recovery (design §3 Sugg. 5, PRD F10).

Spawned by the FastMCP lifespan as a background task after a 2 s sleep, so it never blocks the
initialize handshake. Touches only rows older than STALE_S; every write is compare-and-set.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from payguard.models import IN_FLIGHT, SIGNED_IN_FLIGHT, UNSIGNED_IN_FLIGHT, PaymentStatus, history_entry
from payguard.purchase import CALL_BUDGET_S, _ensure_unknown_finding, reconcile_and_finish

log = logging.getLogger(__name__)
P = PaymentStatus
STALE_S = 60.0
# A live call's rows are never older than ~CALL_BUDGET_S (45 s) + slack, so any in-flight row older
# than STALE_S belongs to a dead process. Rows younger than that may still belong to a process that
# just died, so they get exactly one re-check once they would have turned stale.
RECHECK_MARGIN_S = 1.0


def _phase1(deps: Any, stuck: list[Any]) -> list[str]:
    """Compare-and-set writes only (fast). Returns ids of rows moved to SETTLEMENT_UNKNOWN."""
    repo = deps.repo
    unknown: list[str] = []
    for row in stuck:
        st = row.payment_status
        try:
            if st in UNSIGNED_IN_FLIGHT:
                if repo.update_status(row.purchase_id, st, P.ABANDONED,
                                      history_entry("ABANDONED", by="startup_recovery", was=str(st))):
                    log.info("startup recovery: %s %s -> ABANDONED (nothing was signed)", row.purchase_id, st)
            elif st in SIGNED_IN_FLIGHT:
                if repo.update_status(row.purchase_id, st, P.SETTLEMENT_UNKNOWN,
                                      history_entry("SETTLEMENT_UNKNOWN", by="startup_recovery", reason=f"found {st} after restart")):
                    _ensure_unknown_finding(repo, repo.get_purchase(row.purchase_id))
                    unknown.append(row.purchase_id)
                    log.info("startup recovery: %s %s -> SETTLEMENT_UNKNOWN", row.purchase_id, st)
        except Exception:  # noqa: BLE001 - one bad row must not abort the pass (CancelledError still propagates)
            log.exception("startup recovery: could not process %s", row.purchase_id)
    return unknown


async def _phase2(deps: Any, purchase_ids: list[str]) -> None:
    for pid in purchase_ids:
        try:
            await reconcile_and_finish(deps, deps.repo.get_purchase(pid), time.monotonic() + CALL_BUDGET_S)
        except Exception:  # noqa: BLE001
            log.exception("startup recovery: reconcile failed for %s", pid)


async def _pass(deps: Any) -> None:
    await _phase2(deps, _phase1(deps, deps.repo.list_stuck(STALE_S)))


async def run_startup_recovery(deps: Any) -> None:
    repo = deps.repo
    stuck = repo.list_stuck(STALE_S)
    handled = {r.purchase_id for r in stuck}
    young = [r for r in repo.list_stuck(0) if r.purchase_id not in handled and r.payment_status in IN_FLIGHT]
    if not stuck and not young:
        log.info("startup recovery: nothing to do")
        return
    await _phase2(deps, _phase1(deps, stuck))
    if young:
        now = time.time()
        youngest_age = min(now - r.updated_at for r in young)
        # clamp: a wall clock stepped backwards (future updated_at) must not make the wait unbounded
        wait = min(max(STALE_S - youngest_age, 0.0), STALE_S) + RECHECK_MARGIN_S
        log.info("startup recovery: %d young in-flight row(s); re-checking in %.0fs", len(young), wait)
        await deps.sleep(wait)
        await _pass(deps)
