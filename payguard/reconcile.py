"""Design §5 reconciliation for SETTLEMENT_UNKNOWN rows. Never creates a new payment.

Order per call: poll the chain (≤5 × 2 s) →
  consumed nonce + AuthorizationUsed + matching Transfer: SETTLED (tx hash recorded);
  consumed nonce + AuthorizationCanceled: PAYMENT_FAILED;
  consumed nonce but unproven: stay SETTLEMENT_UNKNOWN, no resend, human review;
  not consumed and past validBefore (+30 s, by block timestamp): PAYMENT_FAILED;
  not consumed and still valid: ONE resend of the identical stored header, classified by the step-6 rules
  without re-entering reconcile → if still unknown, ONE more poll → stop.
The signature of reconcile() is frozen (purchase.reconcile_and_finish calls it).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from payguard import chain
from payguard.chain import ChainState
from payguard.models import (
    EvidenceKind,
    FindingCode,
    NextAction,
    PaymentStatus,
    Purchase,
    history_entry,
)
from payguard.outcome import classify_outcome, send_paid_request

log = logging.getLogger(__name__)
P = PaymentStatus
POLL_ATTEMPTS = 5
POLL_INTERVAL_S = 2.0
EXPIRY_MARGIN_S = 30
RESEND_TIMEOUT_S = 8.0  # review S4: keeps the worst case under the 45 s call budget
MIN_READ_S = 0.5  # a chain read is skipped when less than this remains of the call budget


def _payment_payload(repo: Any, purchase_id: str) -> dict[str, Any] | None:
    rows = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.PAYMENT_PAYLOAD]
    return rows[-1].payload if rows else None


def _last_chain_evidence(repo: Any, purchase_id: str) -> list[str]:
    return [e.evidence_id for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.CHAIN_CHECK][-1:]


async def _poll(deps: Any, reader: Any, row: Purchase, pp: dict[str, Any], deadline: float,
                attempts: int = POLL_ATTEMPTS) -> ChainState | None:
    last: ChainState | None = None
    polls = 0
    for i in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining < MIN_READ_S:
            break
        token = chain.CALL_DEADLINE.set(deadline)  # the real reader's worker thread stops its log search here
        try:  # review S4: an RPC read never runs past the call budget
            last = await asyncio.wait_for(
                reader(row.network, pp["from"], pp["nonce"], pp["to"], int(pp["value"]), row.created_at),
                timeout=remaining)
        except TimeoutError:
            log.warning("chain read timed out for %s", row.purchase_id)
            break
        except Exception as e:  # noqa: BLE001  # an RPC failure is "no information", never a decision
            log.warning("chain read failed for %s: %s: %s", row.purchase_id, type(e).__name__, e)
            break
        finally:
            chain.CALL_DEADLINE.reset(token)
        polls += 1
        if last.used:
            break
        if i < attempts - 1:
            await deps.sleep(max(0.0, min(POLL_INTERVAL_S, deadline - time.monotonic())))
    if last is not None:
        deps.repo.add_evidence(row.purchase_id, EvidenceKind.CHAIN_CHECK, {
            "used": last.used, "event": last.event, "tx_hash": last.tx_hash, "transfer_ok": last.transfer_ok,
            "block_number": last.block_number, "block_timestamp": last.block_timestamp, "polls": polls,
            "from": pp["from"], "nonce": pp["nonce"], "to": pp["to"], "value": pp["value"], "network": row.network})
        deps.repo.append_history(row.purchase_id, history_entry("chain_check", used=last.used, event=last.event,
                                                                 block=last.block_number, polls=polls))
    return last


def _settle_from_chain(repo: Any, row: Purchase, state: ChainState) -> Purchase:
    if not repo.update_status(row.purchase_id, P.SETTLEMENT_UNKNOWN, P.SETTLED,
                              history_entry("SETTLED", via="chain_check", tx=state.tx_hash, block=state.block_number),
                              settlement_tx=state.tx_hash):
        log.warning("compare-and-set lost for %s (SETTLED via chain); leaving row as is", row.purchase_id)
        return repo.get_purchase(row.purchase_id)
    repo.add_evidence(row.purchase_id, EvidenceKind.SETTLEMENT, {
        "source": "chain:AuthorizationUsed+Transfer", "transaction": state.tx_hash, "block_number": state.block_number,
        "block_timestamp": state.block_timestamp, "network": row.network})
    repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
    return repo.get_purchase(row.purchase_id)


def _fail(repo: Any, row: Purchase, state: ChainState, valid_before: int, why: str) -> Purchase:
    chain_ev = _last_chain_evidence(repo, row.purchase_id)
    if not repo.update_status(row.purchase_id, P.SETTLEMENT_UNKNOWN, P.PAYMENT_FAILED,
                              history_entry("PAYMENT_FAILED", reason=why, block_timestamp=state.block_timestamp,
                                            valid_before=valid_before)):
        log.warning("compare-and-set lost for %s (PAYMENT_FAILED); leaving row as is", row.purchase_id)
        return repo.get_purchase(row.purchase_id)
    repo.add_finding(row.purchase_id, FindingCode.PAYMENT_FAILED, "proven", why, chain_ev, [NextAction.GET_PURCHASE])
    repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
    return repo.get_purchase(row.purchase_id)


def _consumed_unproven(repo: Any, row: Purchase, state: ChainState) -> Purchase:
    """Nonce consumed but no AuthorizationUsed+Transfer proof: never resend, never retry, ask a human."""
    current = repo.get_purchase(row.purchase_id)
    if current is None or current.payment_status != P.SETTLEMENT_UNKNOWN:
        return current or row  # another call already decided this row
    chain_ev = _last_chain_evidence(repo, row.purchase_id)
    repo.append_history(row.purchase_id, history_entry("nonce_consumed_unproven", event=state.event, tx=state.tx_hash))
    repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)  # replaced by the sharper finding below
    repo.add_finding(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN, "suspected",
                     f"The authorization nonce is consumed on-chain but no matching USDC transfer to {row.pay_to} for "
                     f"{row.authorized_amount_minor} minor was found (event={state.event}, tx={state.tx_hash}). "
                     f"Do not retry: the authorization cannot be reused. Needs human review.",
                     chain_ev, [NextAction.HUMAN_REVIEW, NextAction.RECONCILE])
    return repo.get_purchase(row.purchase_id)


def _decide_consumed(repo: Any, row: Purchase, state: ChainState) -> Purchase:
    if state.event == "used" and state.transfer_ok:
        return _settle_from_chain(repo, row, state)
    if state.event == "canceled":
        return _fail(repo, row, state, row.valid_before or 0,
                     f"authorization was canceled on-chain in {state.tx_hash}; no transfer can ever land")
    return _consumed_unproven(repo, row, state)


async def reconcile(deps: Any, row: Purchase, deadline: float) -> Purchase:
    repo = deps.repo
    row = repo.get_purchase(row.purchase_id) or row
    if row.payment_status != P.SETTLEMENT_UNKNOWN:
        return row
    pp = _payment_payload(repo, row.purchase_id)
    if pp is None:
        log.error("reconcile %s: no payment_payload evidence; leaving for human review", row.purchase_id)
        return row
    service = deps.registry[row.service_id]
    reader = deps.chain_reader or chain.authorization_state
    valid_before = int(pp["validBefore"])

    # 1-2. poll the chain first
    state = await _poll(deps, reader, row, pp, deadline)
    if state is None:
        return repo.get_purchase(row.purchase_id)
    if state.used:
        return _decide_consumed(repo, row, state)
    if state.block_timestamp > valid_before + EXPIRY_MARGIN_S:
        return _fail(repo, row, state, valid_before,
                     f"authorizationState is false and block time {state.block_timestamp} is past validBefore {valid_before} "
                     f"(+{EXPIRY_MARGIN_S}s): the authorization can never land")
    if time.monotonic() + RESEND_TIMEOUT_S >= deadline:
        repo.append_history(row.purchase_id, history_entry("still_unknown", reason="call budget exhausted before resend",
                                                           valid_before=valid_before))
        return repo.get_purchase(row.purchase_id)

    # 3. not consumed and still valid: one resend of the identical stored header (Sugg. 1a)
    method, url, body = service.build_request(row.args)
    timeout_s = max(1.0, min(RESEND_TIMEOUT_S, service.timeout_s, deadline - time.monotonic()))
    async with deps.http_factory(service) as http:
        outcome = await send_paid_request(http, method, url, body, pp["header"], timeout_s)
    repo.add_evidence(row.purchase_id, EvidenceKind.RESEND, {"status": outcome.status, "error": outcome.error,
                                                            "payment_required_error": outcome.payment_required_error()})
    try:
        row = classify_outcome(repo, repo.get_purchase(row.purchase_id), outcome, P.SETTLEMENT_UNKNOWN,
                               deps.artifact_dir, via="resend")
    except Exception as e:  # a classifier bug leaves the row SETTLEMENT_UNKNOWN, never decides money
        log.exception("resend classification failed for %s", row.purchase_id)
        repo.add_evidence(row.purchase_id, EvidenceKind.SELLER_ERROR,
                          {"stage": "classify", "via": "resend", "error": f"{type(e).__name__}: {e}"})
        row = repo.get_purchase(row.purchase_id)
    if row.payment_status == P.SETTLED:
        repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
        return row

    # exactly one more chain read, no further resend (Sugg. 1b)
    state = await _poll(deps, reader, row, pp, deadline, attempts=1)
    if state is not None and state.used:
        return _decide_consumed(repo, row, state)
    if state is not None and state.block_timestamp > valid_before + EXPIRY_MARGIN_S:
        return _fail(repo, row, state, valid_before, "authorization expired by block time after the resend")
    current = repo.get_purchase(row.purchase_id)
    if current is not None and current.payment_status == P.SETTLEMENT_UNKNOWN:
        repo.append_history(row.purchase_id, history_entry("still_unknown", valid_before=valid_before))
        current = repo.get_purchase(row.purchase_id)
    return current or row
