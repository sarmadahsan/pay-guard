"""Agent-filed disputes (design §3 `report_to_seller`, plan Task 8.5).

The buyer agent emails the seller a documented complaint from its own Primitive inbox, with PayGuard's redacted
evidence attached, and later reads the seller's reply. PayGuard arbitrates nothing and moves no money: filing is
free, so the agent may do it unprompted; a reply never resolves a finding on its own (human_review stays).

The signed authorization (payment_payload.header / signature) is a bearer token and never leaves the machine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from payguard.models import EvidenceKind, FindingCode, NextAction, PaymentStatus, Purchase, history_entry
from payguard.primitive import DryRunBackend, PrimitiveBackend, build_backend
from payguard.registry import Service
from payguard.response import dispute_state, explorer_url, usdc  # noqa: F401  (dispute_state re-exported)

log = logging.getLogger(__name__)

DISPUTABLE = frozenset({FindingCode.PAID_NO_DELIVERY, FindingCode.FULFILLMENT_FAILED, FindingCode.PRICE_MISMATCH})
REDACT = frozenset({"header", "signature"})
CONTEXT_KINDS = (EvidenceKind.QUOTE, EvidenceKind.SELECTED_REQUIREMENT, EvidenceKind.SETTLEMENT, EvidenceKind.CHAIN_CHECK,
                 EvidenceKind.PAYMENT_PAYLOAD, EvidenceKind.SELLER_ERROR)
WELL_KNOWN = "/.well-known/x402-seller.json"
BODY_PREVIEW = 400
REPLY_CAP = 4000
AUTO_MIN_REMAINING_S = 5.0  # Task 8.6: skip the in-call auto dispute when less than this is left of the 45 s call budget
AUTO_FAILED = "auto_dispute failed"  # SELLER_ERROR evidence reason; one automatic attempt per purchase
Backend = PrimitiveBackend | DryRunBackend
_FILE_LOCKS: dict[str, asyncio.Lock] = {}  # per purchase: the one-dispute check and the send+record are one critical section


# ---- bundle ---------------------------------------------------------------------------------------------------

def _redact(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind != EvidenceKind.PAYMENT_PAYLOAD:
        return payload
    out = {k: v for k, v in payload.items() if k not in REDACT}
    out["header"] = "<redacted>"
    return out


def build_bundle(deps: Any, purchase_id: str) -> dict[str, Any]:
    """Purchase + unresolved proven findings + the evidence they rest on, signature redacted. Pure: no network."""
    repo = deps.repo
    row = repo.get_purchase(purchase_id)
    if row is None:
        raise ValueError(f"purchase {purchase_id} not found")
    findings = [f for f in repo.findings_for_purchase(purchase_id)
                if f.resolved_at is None and f.code in DISPUTABLE and f.severity == "proven"]
    if not findings:
        raise ValueError(f"purchase {purchase_id} has no unresolved proven finding to dispute "
                         f"(one of {', '.join(sorted(DISPUTABLE))})")
    all_evidence = repo.list_evidence(purchase_id)
    wanted = {eid for f in findings for eid in f.evidence_ids}
    wanted |= {e.evidence_id for e in all_evidence if e.kind in CONTEXT_KINDS}
    evidence = [{"evidence_id": e.evidence_id, "kind": str(e.kind), "created_at": e.created_at,
                 "payload": _redact(e.kind, e.payload)}
                for e in all_evidence if e.evidence_id in wanted and e.kind not in (EvidenceKind.DISPUTE, EvidenceKind.DISPUTE_REPLY)]
    service: Service | None = deps.registry.get(row.service_id)
    buyer = next((e["payload"].get("from") for e in evidence if e["kind"] == EvidenceKind.PAYMENT_PAYLOAD), None)
    paid = row.payment_status == PaymentStatus.SETTLED  # a PRICE_MISMATCH refusal is a quote we declined, not a payment
    return {
        "principal_id": row.principal_id,
        "purchase": {"purchase_id": row.purchase_id, "service_id": row.service_id, "attempt": row.attempt, "args": row.args,
                     "payment_status": str(row.payment_status), "fulfillment_status": str(row.fulfillment_status),
                     "paid": paid,
                     "authorized_amount_minor": row.authorized_amount_minor, "amount_usdc": usdc(row.authorized_amount_minor),
                     "quoted_amount_minor": row.quoted_amount_minor, "quoted_usdc": usdc(row.quoted_amount_minor),
                     "currency": "USDC", "settlement_tx": row.settlement_tx, "created_at": row.created_at,
                     "buyer_address": buyer, "history": row.history},
        "seller": {"pay_to": row.pay_to, "network": row.network, "url_template": service.url_template if service else None,
                   "accepted_price_minor": service.accepted_price_minor if service else None,
                   "accepted_price_usdc": usdc(service.accepted_price_minor) if service else None,
                   "contact_email": service.contact_email if service else None},
        "findings": [f.to_dict() for f in findings],
        "evidence": evidence,
        "explorer_url": explorer_url(row),
    }


# ---- email ----------------------------------------------------------------------------------------------------

def _asks(bundle: dict[str, Any]) -> str:
    codes = {f["code"] for f in bundle["findings"]}
    p, s = bundle["purchase"], bundle["seller"]
    if not p.get("paid"):  # refused before any payment: there is nothing to refund
        accepted = f" (the agreed price is {s['accepted_price_usdc']} USDC)" if s.get("accepted_price_usdc") else ""
        return f"quote the agreed price for this service{accepted}; no payment was made and none will be at the quoted price"
    refund = f"refund {p['amount_usdc']} USDC to the buyer address"
    if p.get("buyer_address"):
        refund += f" {p['buyer_address']}"
    if codes & {FindingCode.PAID_NO_DELIVERY, FindingCode.FULFILLMENT_FAILED}:
        return f"deliver what was purchased, or {refund}"
    return refund  # PRICE_MISMATCH: the overcharge is the whole complaint


def render_email(bundle: dict[str, Any]) -> tuple[str, str]:
    """Plain text, one agent addressing another: facts, on-chain proof, what we ask, and a request to reply."""
    p, s = bundle["purchase"], bundle["seller"]
    checks = [e["payload"] for e in bundle["evidence"]
              if e["kind"] == EvidenceKind.VALIDATOR and e["payload"].get("passed") is False]
    errors = [e["payload"] for e in bundle["evidence"] if e["kind"] == EvidenceKind.SELLER_ERROR]
    proof = bundle.get("explorer_url") or p.get("settlement_tx") or "see the attached settlement evidence"
    if p.get("paid"):
        money = [f"  - Paid: {p['amount_usdc']} USDC on {s['network']} to {s['pay_to']}", f"  - Settlement: {proof}"]
    else:
        quoted = f"{p['quoted_usdc']} USDC" if p.get("quoted_usdc") else "a price PayGuard could not accept"
        accepted = f" against an agreed price of {s['accepted_price_usdc']} USDC" if s.get("accepted_price_usdc") else ""
        money = [f"  - Quoted: {quoted}{accepted} on {s['network']}" + (f" (pay_to {s['pay_to']})" if s.get("pay_to") else ""),
                 "  - Payment: none. PayGuard refused the quote before signing anything; no money moved."]
    lines = [
        "Hello,",
        "",
        "I am the purchasing agent for the buyer of your x402 service and I am writing to dispute a purchase. "
        "You are welcome to answer with your own agent; I will read the reply and keep it with the evidence.",
        "",
        "What happened:",
        f"  - Service: {p['service_id']} (attempt {p['attempt']}); request: {json.dumps(p.get('args') or {}, sort_keys=True)}",
        *money,
        f"  - Delivery status recorded by PayGuard: {p['fulfillment_status']}",
        "",
        "Why this is a dispute:",
    ]
    for f in bundle["findings"]:
        lines.append(f"  - {f['code']} ({f['severity']}): {f['summary']}")
    for c in checks:
        lines.append(f"  - check '{c.get('name')}' failed: expected {c.get('expected')!r}, actual {c.get('actual')!r}")
    for e in errors[-2:]:
        body = str(e.get("body") or "")[:200]
        lines.append(f"  - your service answered HTTP {e.get('status')} after settlement: {body!r}")
    lines += [
        "",
        f"What I ask: {_asks(bundle)}.",
        "",
        (f"The attached payguard-evidence.json holds the quote, the settlement proof, the delivered artifact's hash and "
         f"the failed checks. The signed payment authorization is redacted; the on-chain record is the proof.")
        if p.get("paid") else
        "The attached payguard-evidence.json holds your 402 quote and the option PayGuard selected and refused.",
        "",
        "Please reply to this email with how you will resolve it. I will not pay for this request again."
        if p.get("paid") else "Please reply to this email with how you will resolve it.",
        "",
        f"Reference: {p['purchase_id']}",
    ]
    amount = f"{p['amount_usdc']} USDC" if p.get("paid") else f"quoted {p.get('quoted_usdc') or 'unacceptable price'} USDC, refused"
    subject = f"Dispute: {p['service_id']} purchase {p['purchase_id']} ({amount})"
    return subject, "\n".join(lines)


# ---- contact --------------------------------------------------------------------------------------------------

def seller_origin(service: Service) -> str | None:
    parts = urlsplit(service.url_template)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


async def resolve_contact(deps: Any, service: Service | None) -> tuple[str | None, str]:
    """(address, source). Tries GET {origin}/.well-known/x402-seller.json -> support_email, then Service.contact_email."""
    if service is None:
        return None, "none"
    origin = seller_origin(service)
    if origin:
        try:
            async with deps.http_factory(service) as client:
                resp = await client.get(origin + WELL_KNOWN, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                email = (resp.json() or {}).get("support_email")
                if isinstance(email, str) and "@" in email:
                    return email.strip(), "well_known"
        except Exception as e:  # noqa: BLE001  (seller down or not JSON: fall through to the env fallback)
            log.info("no seller contact at %s%s: %s", origin, WELL_KNOWN, e)
    if service.contact_email:
        return service.contact_email, "registry"
    return None, "none"


# ---- tool bodies ---------------------------------------------------------------------------------------------

def _dispute_result(deps: Any, purchase_id: str, d: dict[str, Any], *, subject: str | None = None, body: str | None = None,
                    evidence_count: int | None = None) -> dict[str, Any]:
    row = deps.repo.get_purchase(purchase_id)
    return {"purchase_id": purchase_id, "dispute_id": d.get("dispute_id"), "to": d.get("to"), "from": d.get("from"),
            "subject": subject if subject is not None else d.get("subject"),
            "body_preview": (body if body is not None else d.get("body_preview") or "")[:BODY_PREVIEW],
            "evidence_count": evidence_count if evidence_count is not None else d.get("evidence_count"),
            "explorer_url": explorer_url(row) if row else None, "backend": d.get("backend"), "sent_at": d.get("sent_at"),
            "filed_by": d.get("filed_by", "agent"), "safe_next_actions": [NextAction.HUMAN_REVIEW, NextAction.CHECK_DISPUTE]}


async def file_dispute(deps: Any, purchase_id: str, backend: Backend | None = None) -> dict[str, Any]:
    """The `file_dispute` tool body: the agent asks for the dispute explicitly. See file_for_purchase."""
    return await file_for_purchase(deps, purchase_id, backend)


async def file_for_purchase(deps: Any, purchase_id: str, backend: Backend | None = None, *,
                            filed_by: str = "agent") -> dict[str, Any]:
    """File the one dispute a purchase may have; shared by the tool and the auto_dispute policy hook.

    One dispute per purchase: a second call returns the one already on record (`already_filed`). Refuses rows with
    nothing to dispute or no seller contact with an `error` key. Raises when the mail backend fails (nothing recorded).
    """
    repo = deps.repo
    if repo.get_purchase(purchase_id) is None:
        return {"error": f"purchase {purchase_id} not found"}
    async with _FILE_LOCKS.setdefault(purchase_id, asyncio.Lock()):  # auto_dispute, recovery and the tool may overlap
        return await _file_locked(deps, purchase_id, backend, filed_by)


async def _file_locked(deps: Any, purchase_id: str, backend: Backend | None, filed_by: str) -> dict[str, Any]:
    repo = deps.repo
    existing = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.DISPUTE]
    if existing:
        return {**_dispute_result(deps, purchase_id, existing[0].payload), "already_filed": True}
    try:
        bundle = build_bundle(deps, purchase_id)
    except ValueError as e:
        return {"error": str(e), "safe_next_actions": [NextAction.GET_PURCHASE]}
    service = deps.registry.get(bundle["purchase"]["service_id"])
    to, source = await resolve_contact(deps, service)
    if not to:
        return {"error": f"no contact address for seller '{bundle['purchase']['service_id']}': it publishes no "
                         f"{WELL_KNOWN} and SELLER_CONTACT_EMAIL is unset", "safe_next_actions": [NextAction.HUMAN_REVIEW]}
    bundle["seller"]["contact_email"] = to
    bundle["seller"]["contact_source"] = source
    subject, body = render_email(bundle)
    backend = backend or build_backend(deps.artifact_dir)
    sent = await backend.send_mail(to=to, subject=subject, body_text=body, bundle=bundle, purchase_id=purchase_id)
    record = {"dispute_id": sent.id, "to": to, "from": sent.from_addr, "subject": subject, "sent_at": sent.sent_at,
              "backend": sent.backend, "contact_source": source, "body_preview": body[:BODY_PREVIEW],
              "evidence_count": len(bundle["evidence"]), "finding_codes": [f["code"] for f in bundle["findings"]],
              "filed_by": filed_by}
    repo.add_evidence(purchase_id, EvidenceKind.DISPUTE, record)
    repo.append_history(purchase_id, history_entry("DISPUTE_FILED", dispute_id=sent.id, to=to, backend=sent.backend,
                                                   filed_by=filed_by, dispute_filed=True))
    log.info("dispute filed for %s by %s via %s to %s (id %s)", purchase_id, filed_by, sent.backend, to, sent.id)
    return _dispute_result(deps, purchase_id, record, subject=subject, body=body, evidence_count=len(bundle["evidence"]))


# ---- auto_dispute (Task 8.6): the mandate files, not the agent ---------------------------------------------------

def _wants_auto_dispute(deps: Any, row: Purchase) -> bool:
    """policy.auto_dispute, an unresolved proven DISPUTABLE finding, no dispute yet, and no earlier failed attempt."""
    if not getattr(deps.policy, "auto_dispute", False):
        return False
    findings = deps.repo.findings_for_purchase(row.purchase_id)
    if not any(f.resolved_at is None and f.severity == "proven" and f.code in DISPUTABLE for f in findings):
        return False
    for e in deps.repo.list_evidence(row.purchase_id):
        if e.kind == EvidenceKind.DISPUTE:
            return False
        if e.kind == EvidenceKind.SELLER_ERROR and e.payload.get("reason") == AUTO_FAILED:
            return False  # one automatic attempt; the agent (or a human) can still call file_dispute
    return True


async def auto_dispute(deps: Any, row: Purchase, deadline: float, backend: Backend | None = None) -> Purchase:
    """Hook run once a purchase row is terminal and its findings are written (purchase, reconcile, startup recovery).

    Never changes the purchase outcome: a failure to send, or a call with too little budget left, is logged and
    recorded as SELLER_ERROR evidence with reason AUTO_FAILED (a terminal row gets no later pass, so the record is
    what tells the agent to call file_dispute itself). Creates no payment. Returns the (re-read) row.
    """
    repo = deps.repo
    pid = row.purchase_id
    try:
        if not _wants_auto_dispute(deps, row):
            return row
        remaining = deadline - time.monotonic()
        if remaining < AUTO_MIN_REMAINING_S:
            raise TimeoutError(f"skipped: {remaining:.1f} s left of the call budget")
        async with asyncio.timeout(remaining - 1.0):
            result = await file_for_purchase(deps, pid, backend, filed_by="policy")
        if "error" in result:
            raise RuntimeError(result["error"])
    except Exception as e:  # noqa: BLE001  (TimeoutError, backend errors, contact failures: the outcome stands)
        log.warning("auto_dispute failed for %s: %s: %s", pid, type(e).__name__, e)
        try:
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "auto_dispute", "reason": AUTO_FAILED,
                                                                "error": f"{type(e).__name__}: {e}"[:500]})
        except Exception:  # noqa: BLE001
            log.exception("could not record the auto_dispute failure for %s", pid)
    return repo.get_purchase(pid) or row


async def check_dispute(deps: Any, purchase_id: str, backend: Backend | None = None) -> dict[str, Any]:
    """Pull the seller's reply into the ledger (idempotent). Surfaces it; never resolves the finding."""
    repo = deps.repo
    if repo.get_purchase(purchase_id) is None:
        return {"error": f"purchase {purchase_id} not found"}
    evidence = repo.list_evidence(purchase_id)
    filed = [e for e in evidence if e.kind == EvidenceKind.DISPUTE]
    if not filed:
        return {"error": f"no dispute filed for purchase {purchase_id}; call file_dispute first",
                "safe_next_actions": [NextAction.GET_PURCHASE]}
    dispute = filed[0].payload
    dispute_id = str(dispute.get("dispute_id"))
    if dispute.get("backend") == "dry_run" and not isinstance(backend, DryRunBackend):
        backend = DryRunBackend(deps.artifact_dir)  # a dispute never sent cannot have a reply at Primitive
    backend = backend or build_backend(deps.artifact_dir)
    seen = {e.payload.get("reply_id") for e in evidence if e.kind == EvidenceKind.DISPUTE_REPLY}
    new = 0
    for reply in await backend.get_replies(dispute_id, purchase_id):
        if reply.reply_id in seen:
            continue
        payload = {**reply.to_dict(), "body_text": reply.body_text[:REPLY_CAP], "dispute_id": dispute_id}
        repo.add_evidence(purchase_id, EvidenceKind.DISPUTE_REPLY, payload)
        repo.append_history(purchase_id, history_entry("DISPUTE_REPLY", dispute_id=dispute_id, reply_id=reply.reply_id,
                                                       sender=reply.from_addr))
        new += 1
    replies = [e.payload for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.DISPUTE_REPLY]
    return {"purchase_id": purchase_id, "dispute_id": dispute_id, "to": dispute.get("to"),
            "status": "replied" if replies else "awaiting_reply", "new_replies": new,
            "replies": [{k: r.get(k) for k in ("reply_id", "from", "received_at", "body_text", "sender_verified")} for r in replies],
            "safe_next_actions": [NextAction.HUMAN_REVIEW]}


__all__ = ["build_bundle", "render_email", "resolve_contact", "dispute_state", "file_dispute", "file_for_purchase",
           "auto_dispute", "check_dispute"]
