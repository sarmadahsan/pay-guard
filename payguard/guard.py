"""Design §3 step 1: intent lookup, duplicate guard, service quarantine.

M2 STUB: an existing key returns its latest attempt unchanged; otherwise proceed with attempt 1.
M3 (Task 3.1) implements the full decision table. The signature below is frozen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from payguard.models import Purchase
from payguard.registry import Service


@dataclass
class IntentDecision:
    action: str  # "proceed" | "return"
    row: Purchase | None = None
    attempt: int = 1
    supersedes: str | None = None
    reconcile_first: bool = False  # caller reconciles `row` outside the lock, then calls again with after_reconcile=True


def resolve_intent(deps: Any, service: Service, args: dict[str, Any], request_fingerprint: str,
                   purchase_intent_id: str, after_reconcile: bool = False) -> IntentDecision:
    rows = deps.repo.find_by_key(deps.policy.principal_id, purchase_intent_id, request_fingerprint)
    if rows:
        return IntentDecision("return", row=max(rows, key=lambda r: r.attempt))
    return IntentDecision("proceed", attempt=1)
