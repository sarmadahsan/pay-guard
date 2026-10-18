"""Startup recovery (design §3 Sugg. 5, PRD F10).

Spawned by the FastMCP lifespan as a background task after a 2 s sleep, so it never blocks the
initialize handshake. Touches only rows older than STALE_S; every write is compare-and-set.

Order: stuck in-flight rows (phase 1 CAS, phase 2 reconcile) -> a bounded retry of SETTLEMENT_UNKNOWN rows an
earlier run left behind (#76; it makes no status write itself, reconcile does its own CAS) -> one re-check of
in-flight rows that were too young to touch.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from payguard.models import (
    IN_FLIGHT,
    SIGNED_IN_FLIGHT,
    UNSIGNED_IN_FLIGHT,
    EvidenceKind,
    FindingCode,
    PaymentStatus,
    Purchase,
    history_entry,
)
from payguard.purchase import CALL_BUDGET_S, _ensure_unknown_finding, reconcile_and_finish

log = logging.getLogger(__name__)
P = PaymentStatus
STALE_S = 60.0
# A live call's rows are never older than ~CALL_BUDGET_S (45 s) + slack, so any in-flight row older
# than STALE_S belongs to a dead process. Rows younger than that may still belong to a process that
# just died, so they get exactly one re-check once they would have turned stale.
RECHECK_MARGIN_S = 1.0
# #76 unknown-retry pass. A row whose earlier reconcile failed (RPC outage) or was cut short by a crash is no longer
# in-flight, so list_stuck never returns it. Startup retries a few of them; any backlog is left for
# reconcile_purchase or the next start, so the pass can never hold the server's background task for long.
UNKNOWN_SCAN_LIMIT = 200  # newest purchases scanned for an unknown row whose finding was never written (crash)
UNKNOWN_RETRY_MAX = 5
UNKNOWN_RETRY_BUDGET_S = 90.0  # monotonic budget for the whole pass
UNKNOWN_MIN_ROW_S = 1.0  # a row is not started with less than this left of the pass budget


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
                    # Queue for reconcile before anything else can fail: list_stuck no longer returns this row.
                    unknown.append(row.purchase_id)
                    log.info("startup recovery: %s %s -> SETTLEMENT_UNKNOWN", row.purchase_id, st)
                    _ensure_unknown_finding(repo, repo.get_purchase(row.purchase_id))
        except Exception:  # noqa: BLE001 - one bad row must not abort the pass (CancelledError still propagates)
            log.exception("startup recovery: could not process %s", row.purchase_id)
    return unknown


async def _phase2(deps: Any, purchase_ids: list[str]) -> None:
    for pid in purchase_ids:
        try:
            await reconcile_and_finish(deps, deps.repo.get_purchase(pid), time.monotonic() + CALL_BUDGET_S)
        except Exception:  # noqa: BLE001
            log.exception("startup recovery: reconcile failed for %s", pid)


def _stale_unknown(row: Purchase | None) -> bool:
    """SETTLEMENT_UNKNOWN and untouched for STALE_S, so no live call can still own it (wall clock: row age only)."""
    return (row is not None and row.payment_status == P.SETTLEMENT_UNKNOWN
            and time.time() - row.updated_at >= STALE_S)


def _unknown_candidates(deps: Any) -> list[Purchase]:
    """Stale SETTLEMENT_UNKNOWN rows of this server's principal, oldest updated_at first. Never raises."""
    repo = deps.repo
    principal = deps.policy.principal_id
    try:
        ids = [f["purchase_id"] for f in repo.list_findings(principal, unresolved_only=True)
               if f["code"] == FindingCode.SETTLEMENT_UNKNOWN]
        # a crash between the status write and the finding write leaves an unknown row with no finding
        ids += [r.purchase_id for r in repo.list_purchases(principal, limit=UNKNOWN_SCAN_LIMIT)
                if r.payment_status == P.SETTLEMENT_UNKNOWN]
        rows = [repo.get_purchase(pid) for pid in dict.fromkeys(ids)]
        # reconcile cannot act on a row with no stored signed payload (it leaves it for human review): skip it so it
        # never uses a retry slot ahead of a row that can be reconciled
        rows = [r for r in rows if _stale_unknown(r) and any(e.kind == EvidenceKind.PAYMENT_PAYLOAD
                                                              for e in repo.list_evidence(r.purchase_id))]
    except Exception:  # noqa: BLE001 - a store error skips this pass; it must not kill the recovery task
        log.exception("startup recovery: could not list SETTLEMENT_UNKNOWN rows")
        return []
    return sorted(rows, key=lambda r: r.updated_at)


async def _retry_unknown(deps: Any, candidates: list[Purchase], exclude: set[str]) -> None:
    """Reconcile up to UNKNOWN_RETRY_MAX stale unknown rows within UNKNOWN_RETRY_BUDGET_S. No status write here."""
    todo = [r for r in candidates if r.purchase_id not in exclude]
    if not todo:
        return
    pass_deadline = time.monotonic() + UNKNOWN_RETRY_BUDGET_S
    attempted = 0
    examined = 0
    for row in todo:
        if attempted >= UNKNOWN_RETRY_MAX or pass_deadline - time.monotonic() < UNKNOWN_MIN_ROW_S:
            break
        examined += 1
        pid = row.purchase_id
        try:
            current = deps.repo.get_purchase(pid)  # phase 2 or a live call may have decided it meanwhile
            if not _stale_unknown(current):
                continue
            attempted += 1
            deadline = min(time.monotonic() + CALL_BUDGET_S, pass_deadline)
            done = await reconcile_and_finish(deps, current, deadline)
            log.info("startup recovery: retried unknown %s -> %s", pid, done.payment_status)
        except Exception:  # noqa: BLE001 - one bad row must not abort the pass (CancelledError still propagates)
            log.exception("startup recovery: unknown retry failed for %s", pid)
    left = len(todo) - examined
    log.info("startup recovery: retried %d SETTLEMENT_UNKNOWN row(s); %d left for reconcile_purchase or the next start",
             attempted, left)


async def _pass(deps: Any) -> None:
    await _phase2(deps, _phase1(deps, deps.repo.list_stuck(STALE_S)))


async def run_startup_recovery(deps: Any) -> None:
    repo = deps.repo
    stuck = repo.list_stuck(STALE_S)
    handled = {r.purchase_id for r in stuck}
    young = [r for r in repo.list_stuck(0) if r.purchase_id not in handled and r.payment_status in IN_FLIGHT]
    unknown = _unknown_candidates(deps)
    if not stuck and not young and not unknown:
        log.info("startup recovery: nothing to do")
        return
    moved = _phase1(deps, stuck)
    await _phase2(deps, moved)
    await _retry_unknown(deps, unknown, set(moved))
    if young:
        now = time.time()
        youngest_age = min(now - r.updated_at for r in young)
        # clamp: a wall clock stepped backwards (future updated_at) must not make the wait unbounded
        wait = min(max(STALE_S - youngest_age, 0.0), STALE_S) + RECHECK_MARGIN_S
        log.info("startup recovery: %d young in-flight row(s); re-checking in %.0fs", len(young), wait)
        await deps.sleep(wait)
        await _pass(deps)
