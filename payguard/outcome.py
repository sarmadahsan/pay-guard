"""Paid request send and design §3 step 6 outcome classification. Shared by purchase.py and reconcile.py."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from x402.http.constants import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import decode_payment_required_header, decode_payment_response_header

from payguard import artifacts
from payguard.models import EvidenceKind, PaymentStatus, Purchase, history_entry

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    kind: str  # "response" | "error"
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased keys
    content: bytes = b""
    content_type: str = ""
    error: str | None = None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def payment_required_error(self) -> str | None:
        """The 402's PAYMENT-REQUIRED.error if decodable. Informational only; never decides money (design §5)."""
        h = self.header(PAYMENT_REQUIRED_HEADER)
        if not h:
            return None
        try:
            return decode_payment_required_header(h).error
        except Exception:  # noqa: BLE001
            return None


async def send_paid_request(http: httpx.AsyncClient, method: str, url: str, body: dict[str, Any] | None,
                            header: str, timeout_s: float) -> Outcome:
    try:
        resp = await http.request(method, url, json=body, headers={PAYMENT_SIGNATURE_HEADER: header}, timeout=timeout_s)
    except httpx.HTTPError as e:
        return Outcome("error", error=f"{type(e).__name__}: {e}")
    return Outcome("response", resp.status_code, {k.lower(): v for k, v in resp.headers.items()},
                   resp.content, resp.headers.get("content-type", ""))


def classify_outcome(repo: Any, row: Purchase, outcome: Outcome, from_state: PaymentStatus,
                     artifact_dir: Path | str, via: str = "send") -> Purchase:
    """Design §3 step 6. Only 2xx + decodable PAYMENT-RESPONSE with success=true is SETTLED.
    Everything else becomes SETTLEMENT_UNKNOWN. A 2xx body is stored as an artifact first."""
    pid = row.purchase_id
    fields: dict[str, Any] = {}
    if outcome.kind == "response" and 200 <= (outcome.status or 0) < 300:
        try:
            art = artifacts.store_artifact(outcome.content, outcome.content_type, pid, Path(artifact_dir))
            repo.add_evidence(pid, EvidenceKind.ARTIFACT, art.evidence())
            fields["artifact_hash"] = art.sha256
        except Exception as e:  # noqa: BLE001  # a hostile body or bad artifact dir must not hide a settlement
            log.exception("artifact store failed for %s", pid)
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                              "reason": "artifact store failed", "detail": f"{type(e).__name__}: {e}"})
        hdr = outcome.header(PAYMENT_RESPONSE_HEADER)
        settle = None
        if hdr:
            try:
                settle = decode_payment_response_header(hdr)
            except Exception as e:  # noqa: BLE001
                repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                                  "reason": "PAYMENT-RESPONSE decode failed", "detail": f"{type(e).__name__}: {e}"})
                reason = "2xx with undecodable PAYMENT-RESPONSE"
            if settle is not None and settle.success:
                repo.add_evidence(pid, EvidenceKind.SETTLEMENT, {"transaction": settle.transaction, "network": settle.network,
                                  "payer": settle.payer, "amount": settle.amount, "via": via})
                if not repo.update_status(pid, from_state, PaymentStatus.SETTLED,
                                          history_entry("SETTLED", via=via, tx=settle.transaction),
                                          settlement_tx=settle.transaction, **fields):
                    log.warning("compare-and-set lost for %s (SETTLED); leaving row as is", pid)
                return repo.get_purchase(pid)
            if settle is not None:
                repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                                  "reason": "PAYMENT-RESPONSE success=false", "payment_response": settle.model_dump(mode="json")})
                reason = f"2xx with success=false ({settle.error_reason})"
        else:
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                              "reason": "2xx without PAYMENT-RESPONSE"})
            reason = "2xx without PAYMENT-RESPONSE"
    elif outcome.kind == "response":
        pr_error = outcome.payment_required_error()
        pay_resp: Any = None
        hdr = outcome.header(PAYMENT_RESPONSE_HEADER)
        if hdr:
            try:
                pay_resp = decode_payment_response_header(hdr).model_dump(mode="json")
            except Exception:  # noqa: BLE001
                pay_resp = {"undecodable": hdr[:64]}
        repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                          "payment_required_error": pr_error, "payment_response": pay_resp,
                          "body": outcome.content[:2000].decode("utf-8", "replace")})
        reason = f"http {outcome.status}" + (f" ({pr_error})" if pr_error else "")
    else:
        repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"error": outcome.error, "via": via})
        reason = outcome.error or "transport error"
    if not repo.update_status(pid, from_state, PaymentStatus.SETTLEMENT_UNKNOWN,
                              history_entry("SETTLEMENT_UNKNOWN", via=via, reason=reason), **fields):
        log.warning("compare-and-set lost for %s (SETTLEMENT_UNKNOWN); leaving row as is", pid)
    return repo.get_purchase(pid)
