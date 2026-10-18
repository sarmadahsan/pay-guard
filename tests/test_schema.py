import itertools
import time

import pytest

from payguard.models import (
    SAFE_NEXT_ACTION_PREFIXES,
    Finding,
    FindingCode,
    FulfillmentStatus,
    PaymentStatus,
    Purchase,
    RejectReason,
)
from payguard.response import safe_next_actions

PAID_TOOLS = ("get_company_report", "get_research_report", "convert_document")
ADMIN_TOOLS = ("list_services", "get_spending_policy", "get_purchase", "list_findings", "list_purchases", "reconcile_purchase")


async def test_mcp_schema_has_no_force_retry_and_no_approve_retry():
    from payguard.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    names = set(tools)
    assert names == set(PAID_TOOLS) | set(ADMIN_TOOLS), names  # a new tool must be classified here before it ships
    assert "approve_retry" not in names and not any("approve" in n or "force" in n for n in names)
    for n in names:
        props = set(tools[n].parameters["properties"])
        assert not any("force" in p.lower() or "retry" in p.lower() for p in props), (n, props)
        if n in PAID_TOOLS:
            assert {"purchase_intent_id", "task_id"} <= props, (n, props)


def test_vocabulary_has_no_pay_or_retry():
    for v in SAFE_NEXT_ACTION_PREFIXES:
        assert "pay" not in v.lower() and "retry" not in v.lower()


@pytest.mark.parametrize("status,fulfillment,codes", list(itertools.product(
    list(PaymentStatus), list(FulfillmentStatus),
    [(), *((c,) for c in FindingCode), (FindingCode.PRICE_MISMATCH, FindingCode.DUPLICATE_SUSPECTED)])))
def test_safe_next_actions_never_pays(status, fulfillment, codes):
    row = Purchase.new(principal_id="p", task_id=None, purchase_intent_id="i", request_fingerprint="f" * 64,
                       service_id="report", args={}, network="eip155:84532")
    row.payment_status, row.fulfillment_status, row.valid_before = status, fulfillment, int(time.time()) + 300
    if status == PaymentStatus.REJECTED:
        row.reject_reason = RejectReason.SERVICE_QUARANTINED
    findings = [Finding(finding_id="f", purchase_id=row.purchase_id, code=c, severity="proven", summary="s",
                       evidence_ids=[], safe_next_actions=[], created_at=time.time()) for c in codes]
    acts = safe_next_actions(row, findings)
    assert acts and all(a.startswith(SAFE_NEXT_ACTION_PREFIXES) for a in acts)
    assert not any("pay" in a.lower() or "retry" in a.lower() for a in acts)
    if status == PaymentStatus.SETTLEMENT_UNKNOWN:
        assert "reconcile_purchase" in acts
    if status == PaymentStatus.REJECTED:
        assert acts == ["human_review"]
    if FindingCode.DUPLICATE_SUSPECTED in codes and status != PaymentStatus.REJECTED:
        assert acts[:2] == ["human_review", "report_to_seller"]
    if FindingCode.SETTLEMENT_NOT_ON_CHAIN in codes and status != PaymentStatus.REJECTED:
        assert acts[:2] == ["human_review", "report_to_seller"]  # wallet audit (#87): a false receipt needs a human
