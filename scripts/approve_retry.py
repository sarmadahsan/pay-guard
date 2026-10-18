"""Human-only approval of a second payment for a superseded purchase (design §3 N2/S8). Not an MCP tool.

Creates attempt = max + 1 for the same (principal, intent, fingerprint) key, lifts the service's
PAID_NO_DELIVERY quarantine, then runs design §3 steps 2-7. The new payment counts against the budget.

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
    from payguard.models import IN_FLIGHT, FindingCode, PaymentStatus, Purchase, history_entry
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
    attempts = repo.find_by_key(prior.principal_id, prior.purchase_intent_id, prior.request_fingerprint)
    for a in attempts:
        if a.payment_status in IN_FLIGHT or a.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
            print(f"refused: attempt {a.attempt} {a.purchase_id} is {a.payment_status}; wait until "
                  f"valid_before={a.valid_before} (unix) and run reconcile_purchase first")
            return 2
    try:
        new = repo.create_purchase(Purchase.new(
            principal_id=prior.principal_id, task_id=prior.task_id, purchase_intent_id=prior.purchase_intent_id,
            request_fingerprint=prior.request_fingerprint, service_id=prior.service_id, args=prior.args,
            network=prior.network, attempt=max(a.attempt for a in attempts) + 1,
            supersedes_purchase_id=prior.purchase_id, approved_by="cli"))
    except DuplicateKeyError:
        print("refused: another approval already took the next attempt number; re-check with list_purchases")
        return 2
    repo.append_history(new.purchase_id, history_entry("APPROVED_BY_CLI", reason=args.reason, supersedes=prior.purchase_id))
    lifted = repo.resolve_service_findings(prior.principal_id, prior.service_id, FindingCode.PAID_NO_DELIVERY)
    print(f"created attempt {new.attempt} ({new.purchase_id}) superseding {prior.purchase_id}; "
          f"resolved {lifted} PAID_NO_DELIVERY finding(s)")
    row = await run_from_quote(deps, new, service, prior.args, time.monotonic() + CALL_BUDGET_S)
    print(json.dumps(build_response(deps, row), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
