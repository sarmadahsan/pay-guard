"""Glanceable, risk-coded view of a purchase: one colour and one line (design §6 presentation layer).

Pure functions over data that response.py already built. Nothing here reads the store or guesses:
`risk` looks only at payment_status, delivery, transactions and findings.

Risk rules, first match wins:
  red   PAYMENT_FAILED; delivery "partial"; SETTLED with delivery "none"; or an unresolved "proven" finding
        on a purchase where money moved or may have moved (non-empty transactions).
  green SETTLED, delivery "verified", and no unresolved findings at all.
  amber everything else: IN_PROGRESS, SETTLEMENT_UNKNOWN, delivery unverified/pending, and refusals
        (REJECTED / ABANDONED) where no money moved.

A refusal is amber, not green: PayGuard did its job (e.g. a price mismatch was blocked), but the task the agent
wanted is not done, so it is worth a glance. A proven finding on a refusal proves why PayGuard refused, not a
loss, so it does not make the refusal red.
"""
from __future__ import annotations

from typing import Any, Literal


Risk = Literal["green", "amber", "red"]
EMOJI: dict[str, str] = {"green": "🟢", "amber": "🟡", "red": "🔴"}
_SUBJECT_MAX = 40


def _field(f: Any, name: str) -> Any:
    return f.get(name) if isinstance(f, dict) else getattr(f, name, None)


def _unresolved(findings: list[Any]) -> list[Any]:
    return [f for f in findings if _field(f, "resolved_at") is None]


def risk(payment_status: str, delivery: dict[str, Any], transactions: list[dict[str, Any]], findings: list[Any]) -> Risk:
    """payment_status is the response label (IN_PROGRESS for in-flight states); findings are Finding or dicts."""
    level = delivery.get("level")
    open_ = _unresolved(findings)
    if payment_status == "PAYMENT_FAILED" or level == "partial":
        return "red"
    if payment_status == "SETTLED" and level == "none":
        return "red"
    if transactions and any(_field(f, "severity") == "proven" for f in open_):
        return "red"
    if payment_status == "SETTLED" and level == "verified" and not open_:
        return "green"
    return "amber"


def subject(args: dict[str, Any] | None) -> str | None:
    """The first string argument (ticker, topic, doc_id), shortened for one line."""
    for v in (args or {}).values():
        if isinstance(v, str) and v.strip():
            s = " ".join(v.split())
            return s if len(s) <= _SUBJECT_MAX else s[: _SUBJECT_MAX - 1] + "…"
    return None


def short_tx(tx: str | None) -> str | None:
    if not tx:
        return None
    return tx if len(tx) <= 12 else f"{tx[:6]}…{tx[-5:]}"


def _delivery_text(delivery: dict[str, Any]) -> str:
    level = delivery.get("level")
    ratio = f"{delivery.get('checks_passed', 0)}/{delivery.get('checks_total', 0)}"
    if level == "verified":
        return f"verified {ratio}"
    if level == "partial":
        failed = delivery.get("failed") or []
        return f"partial {ratio}" + (f" · failed {', '.join(failed)}" if failed else "")
    if level == "unverified":
        return f"unverified ({delivery.get('basis', 'checks could not run')})"
    return "nothing delivered"


def headline(*, payment_status: str, service_id: str, args: dict[str, Any] | None, delivery: dict[str, Any],
             transactions: list[dict[str, Any]], findings: list[Any], reject_reason: str | None = None,
             dispute: dict[str, Any] | None = None) -> str:
    """One line such as '🟢 Paid 0.500000 USDC · report ACME · verified 4/4 · tx 0x17a2…3931e'.

    `dispute` is response.dispute_state(...): it appends '· dispute filed' or '· seller replied'."""
    level = risk(payment_status, delivery, transactions, findings)
    what = " ".join(p for p in (service_id, subject(args)) if p)
    tx = transactions[0] if transactions else {}
    amount = tx.get("amount_usdc")  # already integer-formatted by response.usdc
    if payment_status == "SETTLED":
        parts = [f"Paid {amount} USDC", what, _delivery_text(delivery)]
        if short_tx(tx.get("settlement_tx")):
            parts.append(f"tx {short_tx(tx.get('settlement_tx'))}")
        parts.append("proven on-chain" if tx.get("proof") == "chain_check" else "seller receipt")
    elif payment_status == "SETTLEMENT_UNKNOWN":
        parts = [f"Sent {amount} USDC", what, "settlement unknown", "reconcile before spending again"]
    elif payment_status == "IN_PROGRESS":
        parts = [f"Paying {amount} USDC" if amount else "In progress", what, "not settled yet"]
    elif payment_status == "PAYMENT_FAILED":
        parts = ["Payment failed", what, "no money moved"]
    elif payment_status == "REJECTED":
        parts = ["Refused", what, str(reject_reason or "rejected").lower().replace("_", " "), "nothing spent"]
    elif payment_status == "ABANDONED":
        parts = ["Abandoned", what, "nothing spent"]
    else:
        parts = [payment_status, what]
    if dispute:
        parts.append("seller replied" if dispute.get("status") == "replied" else "dispute filed")
    return f"{EMOJI[level]} " + " · ".join(p for p in parts if p)
