"""MCP response shape (design §6) and safe_next_actions (design §3)."""
from __future__ import annotations

from typing import Any

from payguard import artifacts
from payguard.models import (
    IN_FLIGHT,
    SAFE_NEXT_ACTION_PREFIXES,
    EvidenceKind,
    Finding,
    FindingCode,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    RejectReason,
)

EXPLORER = {"eip155:84532": "https://sepolia.basescan.org", "eip155:8453": "https://basescan.org"}


def safe_next_actions(row: Purchase, findings: list[Finding]) -> list[str]:
    codes = {f.code for f in findings if f.resolved_at is None}
    if row.payment_status == PaymentStatus.REJECTED and row.reject_reason == RejectReason.SERVICE_QUARANTINED:
        acts = [NextAction.HUMAN_REVIEW]
    elif codes & {FindingCode.PAID_NO_DELIVERY, FindingCode.FULFILLMENT_FAILED, FindingCode.DUPLICATE_SUSPECTED}:
        acts = [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER]  # design §3: these three always get this pair
        if row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
            acts.append(NextAction.RECONCILE)  # reconcile_purchase is allowed only while unknown
    elif row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
        acts = [NextAction.RECONCILE, NextAction.GET_PURCHASE]
        if row.valid_before:
            acts.append(NextAction.wait_until(row.valid_before))
    elif row.payment_status == PaymentStatus.SETTLED and row.fulfillment_status == FulfillmentStatus.VERIFIED:
        acts = [NextAction.USE_ARTIFACT]
    elif row.payment_status == PaymentStatus.SETTLED and row.fulfillment_status == FulfillmentStatus.UNVERIFIED:
        acts = [NextAction.HUMAN_REVIEW]
    else:
        acts = [NextAction.GET_PURCHASE]
    for a in acts:  # belt and braces: the vocabulary never contains pay/retry
        assert a.startswith(SAFE_NEXT_ACTION_PREFIXES), a
    return acts


def explorer_url(row: Purchase) -> str | None:
    base = EXPLORER.get(row.network)
    if not base:
        return None
    if row.settlement_tx:
        return f"{base}/tx/{row.settlement_tx}"
    if row.pay_to and row.payment_status == PaymentStatus.SETTLED:
        return f"{base}/address/{row.pay_to}#tokentxns"
    return None


def build_response(deps: Any, row: Purchase) -> dict[str, Any]:
    repo = deps.repo
    findings = repo.findings_for_purchase(row.purchase_id)
    evidence = repo.list_evidence(row.purchase_id)
    verification = [e.payload for e in evidence if e.kind == EvidenceKind.VALIDATOR]
    art = artifacts.load_latest(repo, row.purchase_id)
    status = "IN_PROGRESS" if row.payment_status in IN_FLIGHT else str(row.payment_status)
    settled = row.payment_status == PaymentStatus.SETTLED
    return {
        "purchase_id": row.purchase_id,
        "purchase_intent_id": row.purchase_intent_id,
        "service_id": row.service_id,
        "attempt": row.attempt,
        "supersedes_purchase_id": row.supersedes_purchase_id,
        "payment_status": status,
        "reject_reason": row.reject_reason,
        "history": row.history,
        "settled_amount": row.authorized_amount_minor if settled else None,
        "settled_amount_usdc": f"{row.authorized_amount_minor / 1_000_000:.6f}" if settled and row.authorized_amount_minor is not None else None,
        "currency": "USDC",
        "network": row.network,
        "settlement_tx": row.settlement_tx,
        "explorer_url": explorer_url(row),
        "valid_before": row.valid_before if row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN else None,
        "fulfillment_status": str(row.fulfillment_status),
        "verification": verification,
        "findings": [f.to_dict() for f in findings],
        "artifact": art.summary() if art else None,
        "safe_next_actions": safe_next_actions(row, findings),
    }
