"""Wallet audit (issue #87): flag USDC that left the buyer wallet outside PayGuard, and confirm PayGuard's own
settlements on-chain. Read-only: it never pays, never signs and never changes a payment_status. It only adds
evidence and findings.

State per (principal, network), kept with Repository.save_wallet_state and cleared by reset():
  address, baseline_balance_minor, baseline_block, baseline_ts   the snapshot, taken on the first purchase or audit
  cursor_block    last block whose USDC logs were scanned (starts at baseline_block)
  incoming_minor  USDC received since the baseline, up to cursor_block
  outflows        every USDC Transfer out of the wallet since the baseline up to cursor_block, with the
                  EIP-3009 nonces (AuthorizationUsed) of the same transaction

Cheap check, one balanceOf:
  expected = baseline + incoming - recorded outflows - SETTLED PayGuard amounts not yet matched to an outflow.
  In-flight rows (PAYLOAD_SIGNED / SENT / SETTLEMENT_UNKNOWN) not yet matched are pending, so any balance in
  [expected - pending, expected] is consistent. A row SETTLED less than NOT_ON_CHAIN_GRACE_S ago also counts as
  pending, because a lagging RPC may not show its transfer yet.
Scan, only when the balance is outside that range or a SETTLED row still has no on-chain match (never from a paid
tool): read the logs from cursor_block + 1 to the balance block, then match outflows to purchase rows by
transaction hash or nonce, plus pay_to and amount.
  - an unmatched outflow is a proven UNEXPLAINED_OUTFLOW (in the wallet block; findings rows need a purchase);
  - a SETTLED row since the baseline with no transfer, once the scan covers the balance block, gets a proven
    SETTLEMENT_NOT_ON_CHAIN finding;
  - a matched SETTLED row gets SETTLEMENT evidence with source "chain:...", so its proof becomes on-chain.
Any RPC or store error gives status "unavailable"; nothing here ever fails the tool call.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from payguard import chain
from payguard.models import EvidenceKind, FindingCode, NextAction, PaymentStatus, Purchase, now
from payguard.response import EXPLORER, usdc

log = logging.getLogger(__name__)
P = PaymentStatus
PENDING_STATES = frozenset({P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN})
MATCHABLE_STATES = PENDING_STATES | {P.SETTLED}
NOT_ON_CHAIN_GRACE_S = 60  # a settlement this recent may not be visible on a lagging RPC yet
AUDIT_BUDGET_S = 20.0  # list_purchases: wall-clock bound on the whole audit
BASELINE_BUDGET_S = 3.0  # paid tools: bound on the one balanceOf that takes the baseline
ROW_LIMIT = 10_000
CHAIN_SOURCE = "chain:wallet_audit:Transfer"
NETWORK_NAMES = {"eip155:84532": "Base Sepolia", "eip155:8453": "Base"}


# ---- state ------------------------------------------------------------------------------------------------

def _baseline(address: str, bal: Any) -> dict[str, Any]:
    return {"address": address, "baseline_balance_minor": int(bal.balance_minor), "baseline_block": int(bal.block_number),
            "baseline_ts": now(), "cursor_block": int(bal.block_number), "incoming_minor": 0, "outflows": []}


def _merge(state: dict[str, Any], scan: Any) -> dict[str, Any]:
    """Add one scan's transfers to the state and advance the cursor to what the scan fully covered."""
    seen = {(o["tx"], o["log_index"]) for o in state["outflows"]}
    outflows = list(state["outflows"])
    for t in scan.outgoing:
        if (t.tx_hash.lower(), t.log_index) in seen:
            continue
        outflows.append({"tx": t.tx_hash.lower(), "log_index": int(t.log_index), "block": int(t.block_number),
                         "to": t.to_addr, "value_minor": int(t.value_minor),
                         "nonces": [n.lower() for n in scan.nonces.get(t.tx_hash.lower(), scan.nonces.get(t.tx_hash, []))]})
    incoming = int(state["incoming_minor"]) + sum(int(t.value_minor) for t in scan.incoming)
    return {**state, "outflows": outflows, "incoming_minor": incoming,
            "cursor_block": max(int(state["cursor_block"]), int(scan.scanned_to))}


# ---- matching (pure) ----------------------------------------------------------------------------------------

def _payment_payload(repo: Any, purchase_id: str) -> dict[str, Any] | None:
    rows = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.PAYMENT_PAYLOAD]
    return rows[-1].payload if rows else None


def _matches(o: dict[str, Any], row: Purchase, pp: dict[str, Any] | None) -> bool:
    if not row.pay_to or row.authorized_amount_minor is None:
        return False
    if o["to"].lower() != row.pay_to.lower() or int(o["value_minor"]) != int(row.authorized_amount_minor):
        return False
    by_tx = bool(row.settlement_tx) and o["tx"] == str(row.settlement_tx).lower()
    by_nonce = pp is not None and str(pp.get("nonce", "")).lower() in o["nonces"]
    return by_tx or by_nonce


def match_outflows(outflows: list[dict[str, Any]], candidates: list[tuple[Purchase, dict[str, Any] | None]]
                   ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Each outflow explains at most one row and each row at most one outflow. Returns (purchase_id -> outflow,
    unmatched outflows)."""
    matched: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    for o in sorted(outflows, key=lambda x: (x["block"], x["log_index"])):
        hit = next((row for row, pp in candidates if row.purchase_id not in matched and _matches(o, row, pp)), None)
        if hit is None:
            unmatched.append(o)
        else:
            matched[hit.purchase_id] = o
    return matched, unmatched


def _evaluate(repo: Any, state: dict[str, Any], rows: list[Purchase], bal: Any) -> dict[str, Any]:
    address = state["address"].lower()
    candidates = []
    for r in rows:
        if r.payment_status not in MATCHABLE_STATES:
            continue
        pp = _payment_payload(repo, r.purchase_id)
        if pp is not None and str(pp.get("from", address)).lower() != address:
            continue  # signed by another wallet
        candidates.append((r, pp))
    matched, unexplained = match_outflows(state["outflows"], candidates)
    since = [r for r in rows if r.created_at >= state["baseline_ts"]]
    settled_unmatched = [r for r in since if r.payment_status == P.SETTLED and r.purchase_id not in matched]
    recent = [r for r in settled_unmatched if r.updated_at + NOT_ON_CHAIN_GRACE_S >= bal.block_timestamp]
    pending = sum(int(r.authorized_amount_minor or 0) for r in rows
                  if r.payment_status in PENDING_STATES and r.purchase_id not in matched)
    expected = (int(state["baseline_balance_minor"]) + int(state["incoming_minor"])
                - sum(int(o["value_minor"]) for o in state["outflows"])
                - sum(int(r.authorized_amount_minor or 0) for r in settled_unmatched))
    # pending money may already have left (down to expected - pending); a very recent settlement may not have yet
    balanced = expected - pending <= int(bal.balance_minor) <= expected + sum(int(r.authorized_amount_minor or 0) for r in recent)
    return {"matched": matched, "unexplained": unexplained, "settled_unmatched": settled_unmatched,
            "recent": {r.purchase_id for r in recent}, "pending": pending, "expected": expected,
            "balanced": balanced, "rows": {r.purchase_id: r for r in rows}}


# ---- writes (evidence and findings only) -----------------------------------------------------------------------

def _has_chain_settlement(repo: Any, purchase_id: str) -> bool:
    return any(e.kind == EvidenceKind.SETTLEMENT and str(e.payload.get("source", "")).startswith("chain")
               for e in repo.list_evidence(purchase_id))


def _open_finding(repo: Any, purchase_id: str, code: str) -> bool:
    return any(f.code == code and f.resolved_at is None for f in repo.findings_for_purchase(purchase_id))


def _record(repo: Any, network: str, state: dict[str, Any], view: dict[str, Any], bal: Any) -> None:
    for pid, o in view["matched"].items():
        row = view["rows"][pid]
        if row.payment_status != P.SETTLED:
            continue  # in-flight rows are reconcile's business (CAS); the audit never changes a status
        if not _has_chain_settlement(repo, pid):
            repo.add_evidence(pid, EvidenceKind.SETTLEMENT, {
                "source": CHAIN_SOURCE, "transaction": o["tx"], "block_number": o["block"], "network": network,
                "from": state["address"], "to": o["to"], "value": str(o["value_minor"]),
                "seller_reported_tx": row.settlement_tx})
        repo.resolve_findings(pid, FindingCode.SETTLEMENT_NOT_ON_CHAIN)
    if int(state["cursor_block"]) < int(bal.block_number):
        return  # the scan has not covered the balance block: absence is not proven yet
    for row in view["settled_unmatched"]:
        if row.purchase_id in view["recent"] or _open_finding(repo, row.purchase_id, FindingCode.SETTLEMENT_NOT_ON_CHAIN):
            continue
        ev = repo.add_evidence(row.purchase_id, EvidenceKind.CHAIN_CHECK, {
            "source": "wallet_audit", "network": network, "from": state["address"], "to": row.pay_to,
            "value": str(row.authorized_amount_minor), "seller_reported_tx": row.settlement_tx,
            "scanned_from_block": int(state["baseline_block"]) + 1, "scanned_to_block": int(state["cursor_block"]),
            "transfer_found": False})
        repo.add_finding(row.purchase_id, FindingCode.SETTLEMENT_NOT_ON_CHAIN, "proven",
                         f"PayGuard recorded this purchase as SETTLED ({usdc(row.authorized_amount_minor)} USDC to "
                         f"{row.pay_to}, seller tx {row.settlement_tx}), but no USDC transfer from the buyer wallet "
                         f"matches it in blocks {int(state['baseline_block']) + 1}-{state['cursor_block']}. "
                         f"The seller's receipt is not backed on-chain.",
                         [ev], [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])


# ---- output ---------------------------------------------------------------------------------------------------

def _short(s: str | None) -> str:
    s = s or ""
    return s if len(s) <= 12 else f"{s[:6]}…{s[-4:]}"


def _outflow_finding(network: str, o: dict[str, Any]) -> dict[str, Any]:
    base = EXPLORER.get(network)
    return {"code": FindingCode.UNEXPLAINED_OUTFLOW.value, "severity": "proven", "network": network,
            "tx": o["tx"], "to": o["to"], "amount_minor": int(o["value_minor"]), "amount_usdc": usdc(int(o["value_minor"])),
            "block_number": o["block"], "explorer_url": f"{base}/tx/{o['tx']}" if base else None,
            "summary": f"{usdc(int(o['value_minor']))} USDC left the buyer wallet to {o['to']} in {o['tx']} "
                       f"(block {o['block']}) and no PayGuard purchase made that payment.",
            "safe_next_actions": [NextAction.HUMAN_REVIEW]}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _network_block(repo: Any, network: str, state: dict[str, Any], view: dict[str, Any], bal: Any) -> dict[str, Any]:
    not_on_chain = [pid for pid in view["rows"] if _open_finding(repo, pid, FindingCode.SETTLEMENT_NOT_ON_CHAIN)]
    outflows = [_outflow_finding(network, o) for o in view["unexplained"]]
    caught_up = int(state["cursor_block"]) >= int(bal.block_number)
    if outflows or not_on_chain or not view["balanced"]:
        status = "drift"
    else:
        status = "ok"
    reason = None
    if status == "drift" and not outflows and not not_on_chain:
        reason = ("the balance differs from PayGuard's records; the log scan has not caught up yet, call again"
                  if not caught_up else "the balance differs from PayGuard's records and no transfer explains it")
    return {
        "network": network, "address": state["address"], "status": status, "reason": reason,
        "baseline": {"balance_minor": int(state["baseline_balance_minor"]),
                     "balance_usdc": usdc(int(state["baseline_balance_minor"])),
                     "block_number": int(state["baseline_block"]), "taken_at": _iso(float(state["baseline_ts"]))},
        "current": {"balance_minor": int(bal.balance_minor), "balance_usdc": usdc(int(bal.balance_minor)),
                    "block_number": int(bal.block_number)},
        "expected_minor": view["expected"], "expected_usdc": usdc(view["expected"]),
        "pending_minor": view["pending"], "pending_usdc": usdc(view["pending"]),
        "incoming_minor": int(state["incoming_minor"]), "incoming_usdc": usdc(int(state["incoming_minor"])),
        "scanned_to_block": int(state["cursor_block"]),
        "confirmed_on_chain": sorted(pid for pid in view["matched"] if view["rows"][pid].payment_status == P.SETTLED),
        "settlements_not_on_chain": not_on_chain,
        "unexplained_outflows": outflows,
    }


def wallet_headline(status: str, networks: list[dict[str, Any]], outflows: list[dict[str, Any]]) -> str:
    if status == "unavailable":
        return "⚪ Wallet audit unavailable · purchases are not affected"
    not_on_chain = sum(len(n.get("settlements_not_on_chain") or []) for n in networks)
    if outflows:
        o = outflows[0]
        more = f" (+{len(outflows) - 1} more)" if len(outflows) > 1 else ""
        return (f"🔴 Paid outside PayGuard: {o['amount_usdc']} USDC to {_short(o['to'])} · tx {_short(o['tx'])}{more}")
    if not_on_chain:
        return f"🔴 {not_on_chain} recorded settlement(s) not found on-chain · seller receipt not backed"
    if status == "drift":
        return "🟡 Wallet balance differs from PayGuard's records · checking"
    ok = [n for n in networks if n.get("status") == "ok"]
    bal = " · ".join(f"{n['current']['balance_usdc']} USDC" + (f" on {NETWORK_NAMES.get(n['network'], n['network'])}"
                                                               if len(networks) > 1 else "") for n in ok)
    pending = sum(int(n.get("pending_minor") or 0) for n in ok)
    tail = f" · {usdc(pending)} USDC pending" if pending else ""
    down = [NETWORK_NAMES.get(n["network"], n["network"]) for n in networks if n.get("status") == "unavailable"]
    tail += f" · {', '.join(down)} audit unavailable" if down else ""
    return f"✅ Wallet {bal} · all outflows explained by PayGuard purchases{tail}"


# ---- entry points ---------------------------------------------------------------------------------------------

def _configured(deps: Any) -> bool:
    return getattr(deps, "balance_reader", None) is not None and getattr(deps, "buyer_address", None) is not None


def _networks(deps: Any) -> list[str]:
    allowed = set(deps.policy.allowed_services)
    return sorted({s.network for s in deps.registry.values() if s.id in allowed})


def _rows(deps: Any, network: str) -> list[Purchase]:
    return [r for r in deps.repo.list_purchases(deps.policy.principal_id, limit=ROW_LIMIT) if r.network == network]


async def ensure_baseline(deps: Any, network: str) -> None:
    """Paid tools: take the baseline before the first payment. At most one balanceOf, never a log scan. Never raises."""
    if not _configured(deps):
        return
    try:
        if deps.repo.get_wallet_state(deps.policy.principal_id, network) is not None:
            return
        address = deps.buyer_address(network)
        bal = await asyncio.wait_for(deps.balance_reader(network, address), timeout=BASELINE_BUDGET_S)
        deps.repo.save_wallet_state(deps.policy.principal_id, network, _baseline(address, bal), create_only=True)
    except Exception as e:  # noqa: BLE001  # no baseline yet: the first audit takes it instead
        log.warning("wallet baseline skipped for %s: %s: %s", network, type(e).__name__, e)


async def _audit_network(deps: Any, network: str, deadline: float, scan: bool) -> dict[str, Any]:
    repo, principal = deps.repo, deps.policy.principal_id
    address = deps.buyer_address(network)
    bal = await deps.balance_reader(network, address)
    state = repo.get_wallet_state(principal, network)
    if state is None or str(state.get("address", "")).lower() != address.lower():
        state = repo.save_wallet_state(principal, network, _baseline(address, bal), create_only=state is None)
    rows = _rows(deps, network)
    view = _evaluate(repo, state, rows, bal)
    behind = int(state["cursor_block"]) < int(bal.block_number)
    if scan and behind and deps.transfer_reader is not None and (not view["balanced"] or view["settled_unmatched"]):
        result = await deps.transfer_reader(network, address, int(state["cursor_block"]) + 1, int(bal.block_number),
                                            stop_at=deadline)
        state = _merge(state, result)
        repo.save_wallet_state(principal, network, state)
        view = _evaluate(repo, state, rows, bal)
    _record(repo, network, state, view, bal)
    return _network_block(repo, network, state, view, bal)


async def audit_wallet(deps: Any, *, scan: bool = True, budget_s: float = AUDIT_BUDGET_S) -> dict[str, Any]:
    """The `wallet` block of list_purchases. Never raises: any failure is status "unavailable"."""
    if not _configured(deps):
        return {"status": "unavailable", "reason": "wallet audit is not configured", "headline": wallet_headline("unavailable", [], []),
                "networks": [], "unexplained_outflows": []}
    deadline = time.monotonic() + budget_s
    blocks: list[dict[str, Any]] = []
    for network in _networks(deps):
        token = chain.CALL_DEADLINE.set(deadline)  # the real scan's worker thread stops here
        try:
            blocks.append(await asyncio.wait_for(_audit_network(deps, network, deadline, scan),
                                                 timeout=max(0.1, deadline - time.monotonic() + 2.0)))
        except Exception as e:  # noqa: BLE001  # RPC down, key missing, store error: "no information", never a failure
            log.warning("wallet audit unavailable for %s: %s: %s", network, type(e).__name__, e)
            blocks.append({"network": network, "status": "unavailable", "reason": f"{type(e).__name__}",
                           "unexplained_outflows": [], "settlements_not_on_chain": []})
        finally:
            chain.CALL_DEADLINE.reset(token)
    statuses = {b["status"] for b in blocks}
    # one network's RPC being down does not hide another network's result
    status = "drift" if "drift" in statuses else "ok" if "ok" in statuses else "unavailable"
    outflows = [o for b in blocks for o in b.get("unexplained_outflows") or []]
    return {"status": status, "headline": wallet_headline(status, blocks, outflows), "networks": blocks,
            "unexplained_outflows": outflows}
