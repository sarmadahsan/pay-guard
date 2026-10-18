"""Purchase state machine (design §3). Every transition is persisted before the next network call."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from x402.client import x402Client
from x402.http.constants import PAYMENT_REQUIRED_HEADER
from x402.http.utils import (
    decode_payment_required_header,
    detect_payment_required_version,
    encode_payment_signature_header,
)
from x402.schemas import PaymentRequired, PaymentRequirements

from payguard import artifacts, guard, reconcile, verify
from payguard.guard import IntentDecision
from payguard.models import (
    EvidenceKind,
    FindingCode,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    RejectReason,
    fingerprint,
    history_entry,
)
from payguard.outcome import classify_outcome, send_paid_request
from payguard.policy import MandateError, Policy, check_mandate, check_service_allowed
from payguard.registry import Service
from payguard.response import build_response
from payguard.store.base import DuplicateKeyError, Repository

log = logging.getLogger(__name__)
P = PaymentStatus
LOCK = asyncio.Lock()  # process-wide: lookup + guard + row creation, and the budget read + AUTHORIZED write. Never held across network I/O.
CALL_BUDGET_S = 45.0  # review S4: wall-clock budget for one tool call


@dataclass
class Deps:
    repo: Repository
    policy: Policy
    registry: dict[str, Service]
    client_factory: Callable[[str], x402Client]
    http_factory: Callable[[Service], httpx.AsyncClient]
    artifact_dir: Path
    chain_reader: Callable[..., Awaitable[Any]] | None = None  # M5: (network, from, nonce, pay_to, value_minor, since_ts) -> ChainState
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


async def purchase(deps: Deps, service_id: str, args: dict[str, Any], purchase_intent_id: str,
                   task_id: str | None = None) -> dict[str, Any]:
    """Entry point for every paid tool (design §6)."""
    deadline = time.monotonic() + CALL_BUDGET_S
    service = deps.registry.get(service_id)
    if service is None:
        return {"error": f"unknown service '{service_id}'", "safe_next_actions": [NextAction.HUMAN_REVIEW]}
    fp = fingerprint(service_id, args)

    async with LOCK:
        decision, row = _resolve_and_create(deps, service, args, fp, purchase_intent_id, task_id)
    if decision.action == "return" and decision.reconcile_first and decision.row is not None:
        await reconcile_and_finish(deps, decision.row, deadline)  # network I/O: outside the lock
        async with LOCK:
            decision, row = _resolve_and_create(deps, service, args, fp, purchase_intent_id, task_id, after_reconcile=True)
    if decision.action == "return" or row is None:
        assert decision.row is not None
        return build_response(deps, deps.repo.get_purchase(decision.row.purchase_id))

    try:
        check_service_allowed(deps.policy, service)
    except MandateError as e:
        return build_response(deps, _reject(deps.repo, row, P.INTENT_RECORDED, e.code, str(e)))
    row = await run_from_quote(deps, row, service, args, deadline)
    return build_response(deps, row)


def _resolve_and_create(deps: Deps, service: Service, args: dict[str, Any], fp: str, intent_id: str,
                        task_id: str | None, after_reconcile: bool = False) -> tuple[IntentDecision, Purchase | None]:
    """Design §3 step 1 under the lock: decide, then create the row in the same critical section."""
    decision = guard.resolve_intent(deps, service, args, fp, intent_id, after_reconcile=after_reconcile)
    if decision.action != "proceed":
        return decision, None
    row = Purchase.new(principal_id=deps.policy.principal_id, task_id=task_id, purchase_intent_id=intent_id,
                       request_fingerprint=fp, service_id=service.id, args=args, network=service.network,
                       attempt=decision.attempt, supersedes_purchase_id=decision.supersedes)
    try:
        return decision, deps.repo.create_purchase(row)
    except DuplicateKeyError:
        existing = max(deps.repo.find_by_key(deps.policy.principal_id, intent_id, fp), key=lambda r: r.attempt)
        return IntentDecision("return", row=existing), None  # a concurrent call won; response shows IN_PROGRESS


def _reject(repo: Repository, row: Purchase, expected: PaymentStatus, code: str, summary: str,
            finding: bool = False, evidence_ids: list[str] | tuple[str, ...] = ()) -> Purchase:
    repo.update_status(row.purchase_id, expected, P.REJECTED,
                       history_entry("REJECTED", reason=str(code), detail=summary), reject_reason=str(code))
    if finding:
        actions = [NextAction.HUMAN_REVIEW] if str(code) == RejectReason.SERVICE_QUARANTINED else [NextAction.GET_PURCHASE]
        repo.add_finding(row.purchase_id, str(code), "proven", summary, list(evidence_ids), actions)
    return repo.get_purchase(row.purchase_id)


def _lost_race(repo: Repository, row: Purchase) -> Purchase:
    log.warning("compare-and-set lost for %s; aborting this call", row.purchase_id)
    return repo.get_purchase(row.purchase_id)


def clip_timeout(timeout_s: float, deadline: float) -> float:
    """Review S4: no single network wait may run past the 45 s call budget (floor 1 s so a request still goes out)."""
    return max(1.0, min(timeout_s, deadline - time.monotonic()))


def _select_option(pr: PaymentRequired, service: Service) -> PaymentRequirements | None:
    for opt in pr.accepts:
        if opt.scheme == "exact" and opt.network == service.network and str(opt.asset).lower() == service.asset.lower():
            return opt
    return None


async def run_from_quote(deps: Deps, row: Purchase, service: Service, args: dict[str, Any], deadline: float) -> Purchase:
    """Design §3 steps 2–7 on an INTENT_RECORDED row. Also used by scripts/approve_retry.py (M8)."""
    repo = deps.repo
    pid = row.purchase_id
    method, url, body = service.build_request(args)
    async with deps.http_factory(service) as http:
        # ---- 2. quote: unpaid request, version detection, decode, select option
        quote_timeout = clip_timeout(service.timeout_s, deadline)
        try:
            async with asyncio.timeout(quote_timeout):  # absolute bound; httpx's own timeout is per-read inactivity only
                resp = await http.request(method, url, json=body, timeout=quote_timeout)
        except (httpx.HTTPError, TimeoutError) as e:
            # A transient outage is not proof that the route is not x402: no finding, reject reason QUOTE_FAILED.
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "error": f"{type(e).__name__}: {e}"})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.QUOTE_FAILED,
                           f"unpaid request failed before any payment: {type(e).__name__}")
        try:
            version = detect_payment_required_version({k: v for k, v in resp.headers.items()}, resp.content)
        except Exception:  # noqa: BLE001  # ValueError, or AttributeError on a non-object JSON body: both mean "not detected"
            ev = repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "status": resp.status_code,
                                                                     "reason": "no x402 payment-required response"})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.NOT_X402,
                           f"route is not x402-protected (http {resp.status_code} without PAYMENT-REQUIRED)",
                           finding=True, evidence_ids=[ev])
        if version != 2:
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "x402_version": version})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.UNSUPPORTED_X402_VERSION,
                           f"x402 version {version} is not supported (v2 only)")
        try:
            pr = decode_payment_required_header(resp.headers[PAYMENT_REQUIRED_HEADER])
        except Exception as e:  # noqa: BLE001  # missing header (KeyError) or malformed header
            ev = repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "status": resp.status_code,
                                   "reason": "unusable PAYMENT-REQUIRED header", "detail": f"{type(e).__name__}: {e}"})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.NOT_X402,
                           "x402 v2 response without a decodable PAYMENT-REQUIRED header", finding=True, evidence_ids=[ev])
        quote_ev = repo.add_evidence(pid, EvidenceKind.QUOTE, pr.model_dump(mode="json"))
        selected = _select_option(pr, service)
        if selected is None:
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.PRICE_MISMATCH,
                           f"no 'exact' option on {service.network} paying canonical USDC {service.asset}",
                           finding=True, evidence_ids=[quote_ev])
        sel_ev = repo.add_evidence(pid, EvidenceKind.SELECTED_REQUIREMENT, selected.model_dump(mode="json"))
        raw_amount = selected.amount
        if not (isinstance(raw_amount, str) and 1 <= len(raw_amount) <= 30 and raw_amount.isascii() and raw_amount.isdigit()):
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.PRICE_MISMATCH,
                           f"quoted amount {str(raw_amount)[:40]!r} is not a plain integer of minor units",
                           finding=True, evidence_ids=[quote_ev, sel_ev])
        amount = int(raw_amount)
        if not repo.update_status(pid, P.INTENT_RECORDED, P.QUOTED,
                                  history_entry("QUOTED", amount_minor=amount, pay_to=selected.pay_to),
                                  quoted_amount_minor=amount, asset=selected.asset, pay_to=selected.pay_to):
            return _lost_race(repo, row)

        # ---- 3. authorize: mandate under the lock so the budget read and the AUTHORIZED write are atomic
        async with LOCK:
            spent = repo.spent_minor(deps.policy.principal_id)
            try:
                check_mandate(deps.policy, service, amount, selected.asset, spent)
            except MandateError as e:
                return _reject(repo, row, P.QUOTED, e.code, str(e), finding=True, evidence_ids=[quote_ev, sel_ev])
            repo.add_evidence(pid, EvidenceKind.AUTHORIZATION, {
                "amount_minor": amount, "accepted_price_minor": service.accepted_price_minor,
                "spent_before_minor": spent, "remaining_after_minor": deps.policy.total_budget_minor - spent - amount,
                "per_purchase_cap_minor": deps.policy.per_purchase_cap_minor})
            if not repo.update_status(pid, P.QUOTED, P.AUTHORIZED, history_entry("AUTHORIZED", amount_minor=amount),
                                      authorized_amount_minor=amount):
                return _lost_race(repo, row)

        # ---- 4. sign: narrow accepts to the selected option, await, assert, encode, persist, then PAYLOAD_SIGNED
        try:
            client = deps.client_factory(service.network)
            payload = await client.create_payment_payload(pr.model_copy(update={"accepts": [selected]}))
            if payload.accepted != selected:
                raise RuntimeError("SDK selected a different payment option than PayGuard authorized")
            auth = payload.payload["authorization"]
            if int(auth["value"]) != amount:
                raise RuntimeError(f"signed value {auth['value']} != authorized {amount}")
            header = encode_payment_signature_header(payload)
        except Exception as e:  # noqa: BLE001
            log.exception("signing failed for %s", pid)
            return _reject(repo, row, P.AUTHORIZED, RejectReason.SIGNING_FAILED, f"{type(e).__name__}: {e}")
        repo.add_evidence(pid, EvidenceKind.PAYMENT_PAYLOAD, {
            "header": header, "from": auth["from"], "to": auth["to"], "value": auth["value"], "nonce": auth["nonce"],
            "validAfter": auth["validAfter"], "validBefore": auth["validBefore"], "network": service.network})
        valid_before = int(auth["validBefore"])
        if not repo.update_status(pid, P.AUTHORIZED, P.PAYLOAD_SIGNED, history_entry("PAYLOAD_SIGNED", valid_before=valid_before),
                                  valid_before=valid_before):
            return _lost_race(repo, row)

        # ---- 5. send: SENT is persisted BEFORE the paid request leaves (design §3)
        if not repo.update_status(pid, P.PAYLOAD_SIGNED, P.SENT, history_entry("SENT", timeout_s=service.timeout_s)):
            return _lost_race(repo, row)
        outcome = await send_paid_request(http, method, url, body, header, clip_timeout(service.timeout_s, deadline))

    # ---- 6. outcome
    try:
        row = classify_outcome(repo, repo.get_purchase(pid), outcome, P.SENT, deps.artifact_dir, via="send")
    except Exception as e:  # noqa: BLE001  # the row must never stay SENT
        log.exception("classification failed for %s", pid)
        repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "classify", "error": f"{type(e).__name__}: {e}"})
        repo.update_status(pid, P.SENT, P.SETTLEMENT_UNKNOWN,
                           history_entry("SETTLEMENT_UNKNOWN", via="send", reason="classification error"))
        row = repo.get_purchase(pid)
    if row.payment_status == P.SETTLEMENT_UNKNOWN:
        return await reconcile_and_finish(deps, row, deadline)
    # ---- 7. verify
    return _verify(deps, row, service)


async def reconcile_and_finish(deps: Deps, row: Purchase, deadline: float) -> Purchase:
    """Reconcile a SETTLEMENT_UNKNOWN row (design §5), then finish: unknown finding, or step-7 verification."""
    row = await reconcile.reconcile(deps, row, deadline)
    if row.payment_status == P.SETTLEMENT_UNKNOWN:
        _ensure_unknown_finding(deps.repo, row)
        return row
    if row.payment_status == P.SETTLED:
        deps.repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
        return _verify(deps, row, deps.registry[row.service_id])
    return row


def _ensure_unknown_finding(repo: Repository, row: Purchase) -> None:
    if any(f.code == FindingCode.SETTLEMENT_UNKNOWN and f.resolved_at is None for f in repo.findings_for_purchase(row.purchase_id)):
        return
    ev_ids = [e.evidence_id for e in repo.list_evidence(row.purchase_id)
              if e.kind in (EvidenceKind.SELLER_ERROR, EvidenceKind.CHAIN_CHECK, EvidenceKind.RESEND)]
    repo.add_finding(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN, "suspected",
                     f"Payment was sent to '{row.service_id}' but settlement could not be confirmed; "
                     f"the authorization is valid until {row.valid_before}", ev_ids[-3:],
                     [NextAction.RECONCILE, NextAction.GET_PURCHASE])


def _verify(deps: Deps, row: Purchase, service: Service) -> Purchase:
    """Design §3 step 7 / §5 validators. Idempotent: only runs on SETTLED rows with fulfillment PENDING."""
    repo = deps.repo
    if row.payment_status != P.SETTLED or row.fulfillment_status != FulfillmentStatus.PENDING:
        return row
    pid = row.purchase_id
    evidence = repo.list_evidence(pid)
    artifact = artifacts.load_latest(repo, pid)
    if artifact is None and any(e.kind == EvidenceKind.SELLER_ERROR and e.payload.get("reason") == "artifact store failed"
                                for e in evidence):
        ev_ids = [e.evidence_id for e in evidence if e.kind in (EvidenceKind.SETTLEMENT, EvidenceKind.SELLER_ERROR)]
        repo.set_fulfillment(pid, FulfillmentStatus.UNVERIFIED, history_entry("UNVERIFIED", reason="artifact store failed"))
        repo.add_finding(pid, FindingCode.FULFILLMENT_UNVERIFIED, "suspected",
                         f"A body was delivered by '{service.id}' but could not be stored, so it was not verified",
                         ev_ids[-3:], [NextAction.HUMAN_REVIEW])
        return repo.get_purchase(pid)
    if artifact is None:
        proof = [e.evidence_id for e in evidence if e.kind in (EvidenceKind.SETTLEMENT, EvidenceKind.CHAIN_CHECK)]
        repo.set_fulfillment(pid, FulfillmentStatus.NO_DELIVERY, history_entry("NO_DELIVERY"))
        repo.add_finding(pid, FindingCode.PAID_NO_DELIVERY, "proven",
                         f"Settled {row.authorized_amount_minor} minor USDC to '{service.id}' but no artifact was delivered",
                         proof, [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])
        return repo.get_purchase(pid)
    art_ev = [e.evidence_id for e in evidence if e.kind == EvidenceKind.ARTIFACT][-1]
    try:
        results = verify.run_validators(service, row.args, artifact)
    except Exception as e:  # noqa: BLE001
        ev = repo.add_evidence(pid, EvidenceKind.VALIDATOR, {"name": "<runner>", "passed": None, "error": f"{type(e).__name__}: {e}"})
        repo.set_fulfillment(pid, FulfillmentStatus.UNVERIFIED, history_entry("UNVERIFIED", error=f"{type(e).__name__}: {e}"))
        repo.add_finding(pid, FindingCode.FULFILLMENT_UNVERIFIED, "suspected",
                         f"Validator raised {type(e).__name__}: {e}", [ev, art_ev], [NextAction.HUMAN_REVIEW])
        return repo.get_purchase(pid)
    failed: list[tuple[str, str, dict[str, Any]]] = []
    for name, passed, detail in results:
        ev = repo.add_evidence(pid, EvidenceKind.VALIDATOR, {"name": name, "passed": passed, **detail})
        if not passed:
            failed.append((name, ev, detail))
    if failed:
        repo.set_fulfillment(pid, FulfillmentStatus.FAILED, history_entry("FULFILLMENT_FAILED", validators=[n for n, _, _ in failed]))
        for name, ev, detail in failed:
            repo.add_finding(pid, FindingCode.FULFILLMENT_FAILED, "proven",
                             f"Validator '{name}' failed: {detail.get('reason', detail)}", [ev, art_ev],
                             [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])
    else:
        repo.set_fulfillment(pid, FulfillmentStatus.VERIFIED, history_entry("VERIFIED", validators=[n for n, _, _ in results]))
    return repo.get_purchase(pid)
