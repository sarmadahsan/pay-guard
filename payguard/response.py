"""MCP response shape (design §6) and safe_next_actions (design §3)."""
from __future__ import annotations

from typing import Any

from payguard import artifacts, presentation
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


def _usdc(n: int) -> str:
    return f"{n // 1_000_000}.{n % 1_000_000:06d}"


def usdc(minor: int | None) -> str | None:
    """Integer-only USDC formatting (6 decimals); sign-safe for negative remaining budgets."""
    if minor is None:
        return None
    return ("-" if minor < 0 else "") + _usdc(abs(minor))


def _status_label(row: Purchase) -> str:
    return "IN_PROGRESS" if row.payment_status in IN_FLIGHT else str(row.payment_status)


def explorer_url(row: Purchase) -> str | None:
    base = EXPLORER.get(row.network)
    if not base:
        return None
    if row.settlement_tx:
        return f"{base}/tx/{row.settlement_tx}"
    if row.pay_to and row.payment_status == PaymentStatus.SETTLED:
        return f"{base}/address/{row.pay_to}#tokentxns"
    return None


def delivery_summary(row: Purchase, verification: list[dict[str, Any]], findings: list[Finding]) -> dict[str, Any]:
    """A level derived ONLY from checks that ran. Never a guessed probability (schema-valid is not fact-checked)."""
    passed = [v["name"] for v in verification if v.get("passed") is True]
    failed = [v["name"] for v in verification if v.get("passed") is False]
    fs = row.fulfillment_status
    if row.payment_status != PaymentStatus.SETTLED:
        level, basis = "none", "no settled payment"
    elif fs == FulfillmentStatus.VERIFIED:
        level, basis = "verified", "all checks passed"
    elif fs == FulfillmentStatus.FAILED:
        level, basis = "partial", "artifact delivered but failed: " + (", ".join(failed) or "validators failed")
    elif fs == FulfillmentStatus.NO_DELIVERY:
        level, basis = "none", "settled with no artifact"
    elif fs == FulfillmentStatus.PENDING:
        level, basis = "unverified", "checks pending"
    else:
        level, basis = "unverified", "checks could not run"
    return {"level": level, "basis": basis, "checks_passed": len(passed), "checks_total": len(verification), "failed": failed,
            "artifact_sha256": row.artifact_hash,
            "proven_findings": [f.code for f in findings if f.severity == "proven" and f.resolved_at is None]}


def transaction_summary(row: Purchase, evidence: list[Any]) -> list[dict[str, Any]]:
    """What money moved, or may have moved, for this purchase. Empty when nothing was ever sent."""
    base = {"amount_minor": row.authorized_amount_minor, "amount_usdc": usdc(row.authorized_amount_minor),
            "network": row.network, "pay_to": row.pay_to, "settlement_tx": row.settlement_tx, "block_number": None,
            "explorer_url": explorer_url(row), "valid_before": row.valid_before}
    if row.payment_status == PaymentStatus.SETTLED:
        # Chain-proven settlements are recorded as SETTLEMENT evidence with source "chain:AuthorizationUsed+Transfer"
        # and a block_number by the M5 reconcile (Task 5.2); proof and block_number read that evidence.
        settle = [e.payload for e in evidence if e.kind == EvidenceKind.SETTLEMENT]
        last = settle[-1] if settle else {}
        proof = "chain_check" if str(last.get("source", "")).startswith("chain") else "seller_receipt"
        return [{**base, "status": "settled", "proof": proof, "block_number": last.get("block_number")}]
    if row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
        return [{**base, "status": "unknown", "proof": "pending", "explorer_url": None}]
    if row.payment_status in (PaymentStatus.PAYLOAD_SIGNED, PaymentStatus.SENT):
        return [{**base, "status": "in_progress", "proof": "pending", "explorer_url": None}]
    return []  # REJECTED, PAYMENT_FAILED, ABANDONED, or nothing signed yet: no money moved


def budget_summary(deps: Any) -> dict[str, Any]:
    spent = deps.repo.spent_minor(deps.policy.principal_id)
    total = deps.policy.total_budget_minor
    return {"total_budget_minor": total, "spent_minor": spent, "remaining_minor": total - spent,
            "spent_usdc": usdc(spent), "remaining_usdc": usdc(total - spent)}


def compact_purchase(deps: Any, row: Purchase) -> dict[str, Any]:
    """One line per purchase for list_purchases."""
    evidence = deps.repo.list_evidence(row.purchase_id)
    verification = [e.payload for e in evidence if e.kind == EvidenceKind.VALIDATOR]
    findings = deps.repo.findings_for_purchase(row.purchase_id)
    tx = transaction_summary(row, evidence)
    delivery = delivery_summary(row, verification, findings)
    status = _status_label(row)
    return {"purchase_id": row.purchase_id, "purchase_intent_id": row.purchase_intent_id, "service_id": row.service_id,
            "attempt": row.attempt, "payment_status": status,
            "fulfillment_status": str(row.fulfillment_status),
            "delivery_level": delivery["level"],
            "amount_usdc": usdc(row.authorized_amount_minor), "settlement_tx": row.settlement_tx,
            "proof": tx[0]["proof"] if tx else None, "explorer_url": explorer_url(row),
            "unresolved_findings": [f.code for f in findings if f.resolved_at is None], "created_at": row.created_at,
            "failed_checks": delivery["failed"], **_presentation(row, status, delivery, tx, findings)}


def _presentation(row: Purchase, status: str, delivery: dict[str, Any], tx: list[dict[str, Any]],
                  findings: list[Finding]) -> dict[str, Any]:
    return {"risk": presentation.risk(status, delivery, tx, findings),
            "headline": presentation.headline(payment_status=status, service_id=row.service_id, args=row.args,
                                              delivery=delivery, transactions=tx, findings=findings,
                                              reject_reason=row.reject_reason)}


def build_response(deps: Any, row: Purchase) -> dict[str, Any]:
    repo = deps.repo
    findings = repo.findings_for_purchase(row.purchase_id)
    evidence = repo.list_evidence(row.purchase_id)
    verification = [e.payload for e in evidence if e.kind == EvidenceKind.VALIDATOR]
    art = artifacts.load_latest(repo, row.purchase_id)
    status = _status_label(row)
    settled = row.payment_status == PaymentStatus.SETTLED
    tx = transaction_summary(row, evidence)
    delivery = delivery_summary(row, verification, findings)
    return {
        **_presentation(row, status, delivery, tx, findings),
        "purchase_id": row.purchase_id,
        "purchase_intent_id": row.purchase_intent_id,
        "service_id": row.service_id,
        "attempt": row.attempt,
        "supersedes_purchase_id": row.supersedes_purchase_id,
        "payment_status": status,
        "reject_reason": row.reject_reason,
        "history": row.history,
        "settled_amount": row.authorized_amount_minor if settled else None,
        "settled_amount_usdc": _usdc(row.authorized_amount_minor) if settled and row.authorized_amount_minor is not None else None,
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
        "transactions": tx,
        "delivery": delivery,
        "budget": budget_summary(deps),
    }
