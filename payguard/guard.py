"""Design §3 step 1: intent lookup, service quarantine (S3), duplicate guard, same-intent retry (S1).

Runs under the process lock. DB work only: no network. When a SETTLEMENT_UNKNOWN row must be
reconciled first, the decision says reconcile_first=True; the caller reconciles outside the lock
and calls again with after_reconcile=True.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from payguard.models import (
    IN_FLIGHT,
    EvidenceKind,
    FindingCode,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    RejectReason,
    history_entry,
    iso_now,
    now,
)
from payguard.registry import Service

log = logging.getLogger(__name__)
P = PaymentStatus
DUPLICATE_WINDOW_S = 15 * 60


@dataclass
class IntentDecision:
    action: str  # "proceed" | "return"
    row: Purchase | None = None
    attempt: int = 1
    supersedes: str | None = None
    reconcile_first: bool = False  # caller reconciles `row` outside the lock, then calls again with after_reconcile=True


def _trips_guard(row: Purchase) -> bool:
    """Rows that must not be paid for again: in flight, unknown, or settled but not verified."""
    if row.payment_status in IN_FLIGHT or row.payment_status == P.SETTLEMENT_UNKNOWN:
        return True
    return row.payment_status == P.SETTLED and row.fulfillment_status != FulfillmentStatus.VERIFIED


def _mark_duplicate(deps: Any, row: Purchase, variant: str, purchase_intent_id: str) -> None:
    """S1: one DUPLICATE_SUSPECTED finding per row, one retry_blocked evidence row per hit."""
    repo = deps.repo
    ev = repo.add_evidence(row.purchase_id, EvidenceKind.RETRY_BLOCKED,
                           {"variant": variant, "purchase_intent_id": purchase_intent_id, "at": iso_now(),
                            "payment_status": str(row.payment_status), "fulfillment_status": str(row.fulfillment_status)})
    if not repo.has_finding(row.purchase_id, FindingCode.DUPLICATE_SUSPECTED):
        repo.add_finding(row.purchase_id, FindingCode.DUPLICATE_SUSPECTED, "suspected",
                         f"A repeat of purchase {row.purchase_id} ({row.payment_status}/{row.fulfillment_status}) was blocked; "
                         f"no second payment was made", [ev], [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])
    repo.append_history(row.purchase_id, history_entry("retry_blocked", variant=variant, purchase_intent_id=purchase_intent_id))


def _quarantine(deps: Any, service: Service, args: dict[str, Any], fp: str, purchase_intent_id: str, attempt: int,
                supersedes: str | None) -> Purchase:
    """S3: create the row and reject it before any quote. The finding cites the unresolved PAID_NO_DELIVERY findings."""
    repo = deps.repo
    row = repo.create_purchase(Purchase.new(principal_id=deps.policy.principal_id, task_id=None, purchase_intent_id=purchase_intent_id,
                                            request_fingerprint=fp, service_id=service.id, args=args, network=service.network,
                                            attempt=attempt, supersedes_purchase_id=supersedes))
    prior = [f for f in repo.list_findings(deps.policy.principal_id, unresolved_only=True)
             if f["code"] == FindingCode.PAID_NO_DELIVERY and f["service_id"] == service.id]
    summary = (f"Service '{service.id}' is quarantined: {len(prior)} unresolved PAID_NO_DELIVERY finding(s) "
               f"(purchase {prior[0]['purchase_id'] if prior else '?'}). No quote was requested and nothing was paid.")
    # Every finding references at least one evidence row on ITS OWN purchase (PRD non-functional).
    own_ev = repo.add_evidence(row.purchase_id, EvidenceKind.RETRY_BLOCKED, {
        "variant": "quarantined", "service_id": service.id, "at": iso_now(),
        "blocking_findings": [{"finding_id": f["finding_id"], "purchase_id": f["purchase_id"], "evidence_ids": f["evidence_ids"]} for f in prior]})
    evidence_ids = [own_ev]
    repo.update_status(row.purchase_id, P.INTENT_RECORDED, P.REJECTED,
                       history_entry("REJECTED", reason=str(RejectReason.SERVICE_QUARANTINED), detail=summary),
                       reject_reason=str(RejectReason.SERVICE_QUARANTINED))
    repo.add_finding(row.purchase_id, FindingCode.SERVICE_QUARANTINED, "proven", summary, evidence_ids, [NextAction.HUMAN_REVIEW])
    return repo.get_purchase(row.purchase_id)


def resolve_intent(deps: Any, service: Service, args: dict[str, Any], request_fingerprint: str,
                   purchase_intent_id: str, after_reconcile: bool = False) -> IntentDecision:
    repo = deps.repo
    principal = deps.policy.principal_id
    attempt, supersedes = 1, None

    # ---- existing key: look at the latest attempt (design §3 step 1, first bullet) ----
    rows = repo.find_by_key(principal, purchase_intent_id, request_fingerprint)
    if rows:
        latest = max(rows, key=lambda r: r.attempt)
        st = latest.payment_status
        if st == P.SETTLEMENT_UNKNOWN and not after_reconcile:
            return IntentDecision("return", row=latest, reconcile_first=True)
        if st in (P.PAYMENT_FAILED, P.ABANDONED) or (st == P.REJECTED and latest.reject_reason == RejectReason.SERVICE_QUARANTINED):
            attempt, supersedes = latest.attempt + 1, latest.purchase_id  # proven: no money moved (Sugg. 6); a lifted quarantine may retry
        elif _trips_guard(latest):
            # In flight, unknown, or settled-not-verified: PRD F7 / S1 attach DUPLICATE_SUSPECTED on every variant.
            # For an in-flight row the response still shows IN_PROGRESS (response.py maps IN_FLIGHT states).
            _mark_duplicate(deps, latest, "same_intent", purchase_intent_id)
            return IntentDecision("return", row=repo.get_purchase(latest.purchase_id))
        else:
            return IntentDecision("return", row=latest)  # SETTLED+VERIFIED or REJECTED: unchanged

    # ---- duplicate guard on (principal, fingerprint) within 15 minutes, any intent id ----
    # Runs BEFORE the quarantine so a same-topic retry under a new intent id returns the EXISTING record with
    # DUPLICATE_SUSPECTED (PRD metric 4, user story); only a reworded request reaches the quarantine.
    if attempt == 1:
        for cand in repo.find_recent_by_fingerprint(principal, request_fingerprint, now() - DUPLICATE_WINDOW_S):
            if cand.purchase_intent_id == purchase_intent_id:
                continue
            if not _trips_guard(cand):
                continue  # a verified purchase under a fresh intent pays again by design (Sugg. 9)
            if cand.payment_status == P.SETTLEMENT_UNKNOWN and not after_reconcile:
                return IntentDecision("return", row=cand, reconcile_first=True)
            _mark_duplicate(deps, cand, "new_intent", purchase_intent_id)
            return IntentDecision("return", row=repo.get_purchase(cand.purchase_id))

    # ---- service quarantine (S3): before any quote for a new row ----
    if repo.has_unresolved_service_finding(principal, service.id, FindingCode.PAID_NO_DELIVERY):
        row = _quarantine(deps, service, args, request_fingerprint, purchase_intent_id, attempt, supersedes)
        return IntentDecision("return", row=row)

    return IntentDecision("proceed", attempt=attempt, supersedes=supersedes)
