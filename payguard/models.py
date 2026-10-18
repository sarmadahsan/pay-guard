"""Core types of the purchase state machine (design §3). Money is always integer minor units."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class PaymentStatus(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    QUOTED = "QUOTED"
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    PAYLOAD_SIGNED = "PAYLOAD_SIGNED"
    SENT = "SENT"
    SETTLED = "SETTLED"
    SETTLEMENT_UNKNOWN = "SETTLEMENT_UNKNOWN"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    ABANDONED = "ABANDONED"


class FulfillmentStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"
    NO_DELIVERY = "NO_DELIVERY"


P = PaymentStatus
IN_FLIGHT = frozenset({P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT})
BUDGET_STATES = frozenset({P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN, P.SETTLED})
UNSIGNED_IN_FLIGHT = frozenset({P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED})  # recovery -> ABANDONED
SIGNED_IN_FLIGHT = frozenset({P.PAYLOAD_SIGNED, P.SENT})  # recovery -> SETTLEMENT_UNKNOWN

# Design §3 transition table. SETTLEMENT_UNKNOWN -> SETTLEMENT_UNKNOWN is the resend classification no-op.
TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    P.INTENT_RECORDED: frozenset({P.QUOTED, P.REJECTED, P.ABANDONED}),
    P.QUOTED: frozenset({P.AUTHORIZED, P.REJECTED, P.ABANDONED}),
    P.AUTHORIZED: frozenset({P.PAYLOAD_SIGNED, P.REJECTED, P.ABANDONED}),
    P.PAYLOAD_SIGNED: frozenset({P.SENT, P.SETTLEMENT_UNKNOWN}),
    P.SENT: frozenset({P.SETTLED, P.SETTLEMENT_UNKNOWN}),
    P.SETTLEMENT_UNKNOWN: frozenset({P.SETTLED, P.PAYMENT_FAILED, P.SETTLEMENT_UNKNOWN}),
    P.SETTLED: frozenset(),
    P.REJECTED: frozenset(),
    P.PAYMENT_FAILED: frozenset(),
    P.ABANDONED: frozenset(),
}


def assert_transition(expected: PaymentStatus, new: PaymentStatus) -> None:
    if new not in TRANSITIONS[expected]:
        raise ValueError(f"illegal transition {expected} -> {new}")


class FindingCode(StrEnum):
    PRICE_MISMATCH = "PRICE_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NOT_X402 = "NOT_X402"
    SERVICE_QUARANTINED = "SERVICE_QUARANTINED"
    SETTLEMENT_UNKNOWN = "SETTLEMENT_UNKNOWN"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAID_NO_DELIVERY = "PAID_NO_DELIVERY"
    FULFILLMENT_FAILED = "FULFILLMENT_FAILED"
    FULFILLMENT_UNVERIFIED = "FULFILLMENT_UNVERIFIED"
    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"
    UNEXPLAINED_OUTFLOW = "UNEXPLAINED_OUTFLOW"  # wallet audit: USDC left the buyer wallet with no matching purchase
    SETTLEMENT_NOT_ON_CHAIN = "SETTLEMENT_NOT_ON_CHAIN"  # wallet audit: SETTLED row with no transfer on-chain


class RejectReason(StrEnum):
    NOT_X402 = "NOT_X402"
    UNSUPPORTED_X402_VERSION = "UNSUPPORTED_X402_VERSION"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SIGNING_FAILED = "SIGNING_FAILED"
    SERVICE_QUARANTINED = "SERVICE_QUARANTINED"
    POLICY_DENIED = "POLICY_DENIED"
    QUOTE_FAILED = "QUOTE_FAILED"  # unpaid request could not be made (seller down); nothing signed, no proven finding


class EvidenceKind(StrEnum):
    QUOTE = "quote"
    SELECTED_REQUIREMENT = "selected_requirement"
    AUTHORIZATION = "authorization"
    PAYMENT_PAYLOAD = "payment_payload"
    SETTLEMENT = "settlement"
    SELLER_ERROR = "seller_error"
    CHAIN_CHECK = "chain_check"
    RESEND = "resend"
    RETRY_BLOCKED = "retry_blocked"
    ARTIFACT = "artifact"
    VALIDATOR = "validator"
    DISPUTE = "dispute"  # agent-filed complaint to the seller (Task 8.5); never money
    DISPUTE_REPLY = "dispute_reply"  # the seller's reply, surfaced for a human; never resolves a finding by itself


class NextAction:
    """safe_next_actions vocabulary (design §3). No value means pay or retry."""

    GET_PURCHASE = "get_purchase"
    RECONCILE = "reconcile_purchase"
    HUMAN_REVIEW = "human_review"
    REPORT_TO_SELLER = "report_to_seller"
    USE_ARTIFACT = "use_artifact"

    @staticmethod
    def wait_until(valid_before: int) -> str:
        return f"wait_until:{int(valid_before)}"


SAFE_NEXT_ACTION_PREFIXES = (
    NextAction.GET_PURCHASE,
    NextAction.RECONCILE,
    "wait_until:",
    NextAction.HUMAN_REVIEW,
    NextAction.REPORT_TO_SELLER,
    NextAction.USE_ARTIFACT,
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(service_id: str, args: dict[str, Any]) -> str:
    return hashlib.sha256((service_id + canonical_json(args)).encode()).hexdigest()


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def history_entry(state: str, **detail: Any) -> dict[str, Any]:
    return {"at": iso_now(), "state": state, **detail}


@dataclass
class Purchase:
    purchase_id: str
    principal_id: str
    task_id: str | None
    purchase_intent_id: str
    request_fingerprint: str
    attempt: int
    supersedes_purchase_id: str | None
    approved_by: str | None
    service_id: str
    args: dict[str, Any]
    quoted_amount_minor: int | None
    authorized_amount_minor: int | None
    asset: str | None
    network: str
    pay_to: str | None
    payment_status: PaymentStatus
    fulfillment_status: FulfillmentStatus
    settlement_tx: str | None
    valid_before: int | None
    artifact_hash: str | None
    reject_reason: str | None
    history: list[dict[str, Any]]
    created_at: float
    updated_at: float

    @classmethod
    def new(
        cls,
        *,
        principal_id: str,
        task_id: str | None,
        purchase_intent_id: str,
        request_fingerprint: str,
        service_id: str,
        args: dict[str, Any],
        network: str,
        attempt: int = 1,
        supersedes_purchase_id: str | None = None,
        approved_by: str | None = None,
    ) -> "Purchase":
        t = now()
        return cls(
            purchase_id=new_id(), principal_id=principal_id, task_id=task_id,
            purchase_intent_id=purchase_intent_id, request_fingerprint=request_fingerprint,
            attempt=attempt, supersedes_purchase_id=supersedes_purchase_id, approved_by=approved_by,
            service_id=service_id, args=copy.deepcopy(args), quoted_amount_minor=None, authorized_amount_minor=None,
            asset=None, network=network, pay_to=None, payment_status=P.INTENT_RECORDED,
            fulfillment_status=FulfillmentStatus.PENDING, settlement_tx=None, valid_before=None,
            artifact_hash=None, reject_reason=None,
            history=[history_entry("INTENT_RECORDED", attempt=attempt)], created_at=t, updated_at=t,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["payment_status"] = str(self.payment_status)
        d["fulfillment_status"] = str(self.fulfillment_status)
        return d


@dataclass
class Evidence:
    evidence_id: str
    purchase_id: str
    kind: str
    payload: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    finding_id: str
    purchase_id: str
    code: str
    severity: str  # "proven" | "suspected"
    summary: str
    evidence_ids: list[str]
    safe_next_actions: list[str]
    created_at: float
    resolved_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
