"""Human-only approval of a second payment for a superseded purchase (design §3 N2/S8). Not an MCP tool.

Creates attempt = latest + 1 for the same (principal, intent, fingerprint) key, runs design §3 steps 2-7, and
lifts the service's PAID_NO_DELIVERY quarantine only if the new payment SETTLED. It counts against the budget.

Do not run while the MCP server or another approve_retry is buying: the budget lock is per-process.

Usage: uv run python scripts/approve_retry.py <purchase_id> [--reason "..."]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# `python scripts/approve_retry.py` puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("purchase_id")
    ap.add_argument("--reason", default="operator approved re-purchase")
    args = ap.parse_args()
    from payguard.deps import get_deps
    from payguard.guard import DUPLICATE_WINDOW_S
    from payguard.models import (IN_FLIGHT, UNSIGNED_IN_FLIGHT, FindingCode, PaymentStatus, Purchase, history_entry,
                                 now)
    from payguard.policy import MandateError, check_service_allowed
    from payguard.purchase import CALL_BUDGET_S, run_from_quote
    from payguard.response import build_response
    from payguard.store.base import DuplicateKeyError

    deps = get_deps()
    repo = deps.repo
    prior = repo.get_purchase(args.purchase_id)
    if prior is None:
        print("purchase not found")
        return 1
    service = deps.registry.get(prior.service_id)
    if service is None:
        print(f"error: service {prior.service_id!r} of {prior.purchase_id} is not in the registry")
        return 1
    if prior.principal_id != deps.policy.principal_id:
        print(f"refused: {prior.purchase_id} belongs to another principal")
        return 2
    try:
        check_service_allowed(deps.policy, service)
    except MandateError as e:
        print(f"refused: {e}")
        return 2
    attempts = repo.find_by_key(prior.principal_id, prior.purchase_intent_id, prior.request_fingerprint)
    same_request = repo.find_recent_by_fingerprint(prior.principal_id, prior.request_fingerprint, now() - DUPLICATE_WINDOW_S)
    for a in [*attempts, *same_request]:
        if a.payment_status in IN_FLIGHT or a.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
            when = (f"wait until valid_before={a.valid_before} (unix) and run reconcile_purchase first"
                    if a.valid_before is not None else "run reconcile_purchase or wait for recovery")
            print(f"refused: attempt {a.attempt} {a.purchase_id} is {a.payment_status}; {when}")
            return 2
    latest = max(attempts, key=lambda a: a.attempt)
    if latest.purchase_id != prior.purchase_id:
        print(f"note: {prior.purchase_id} is not the latest attempt; superseding {latest.purchase_id}")
    try:
        new = repo.create_purchase(Purchase.new(
            principal_id=prior.principal_id, task_id=prior.task_id, purchase_intent_id=prior.purchase_intent_id,
            request_fingerprint=prior.request_fingerprint, service_id=prior.service_id, args=prior.args,
            network=service.network, attempt=latest.attempt + 1,
            supersedes_purchase_id=latest.purchase_id, approved_by="cli"))
    except DuplicateKeyError:
        print("refused: another approval already took the next attempt number; re-check with list_purchases")
        return 2
    repo.append_history(new.purchase_id, history_entry("APPROVED_BY_CLI", reason=args.reason, supersedes=latest.purchase_id))
    # Quarantine is enforced only by the agent-facing guard. Lift it only once this retry has SETTLED, and only for the
    # findings that existed before it: a retry that settles without delivery gets its own PAID_NO_DELIVERY in _verify.
    quarantined = [f["purchase_id"] for f in repo.list_findings(prior.principal_id)
                   if f["service_id"] == prior.service_id and f["code"] == FindingCode.PAID_NO_DELIVERY]
    print(f"created attempt {new.attempt} ({new.purchase_id}) superseding {latest.purchase_id}")
    try:
        row = await run_from_quote(deps, new, service, prior.args, time.monotonic() + CALL_BUDGET_S)
    except BaseException:
        cur = repo.get_purchase(new.purchase_id)  # signed rows are left for recovery
        if cur is not None and cur.payment_status in UNSIGNED_IN_FLIGHT:
            repo.update_status(new.purchase_id, cur.payment_status, PaymentStatus.ABANDONED,
                               history_entry("ABANDONED", by="approve_retry", reason="cli aborted"))
        raise
    if row.payment_status == PaymentStatus.SETTLED:
        lifted = sum(repo.resolve_findings(pid, FindingCode.PAID_NO_DELIVERY) for pid in quarantined)
        print(f"settled; resolved {lifted} prior PAID_NO_DELIVERY finding(s)")
    else:
        print(f"not settled ({row.payment_status}); quarantine left in place")
    print(json.dumps(build_response(deps, row), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
