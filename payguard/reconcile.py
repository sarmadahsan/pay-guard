"""Design §5 reconciliation for SETTLEMENT_UNKNOWN rows.

M2 STUB: returns the row unchanged. M5 (Task 5.2) implements the chain poll, the single resend and the
block-timestamp PAYMENT_FAILED rule. The signature below is frozen.
"""
from __future__ import annotations

from typing import Any

from payguard.models import Purchase


async def reconcile(deps: Any, row: Purchase, deadline: float) -> Purchase:
    return deps.repo.get_purchase(row.purchase_id) or row
