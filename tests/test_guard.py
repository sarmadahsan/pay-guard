import time

import pytest

from payguard.guard import resolve_intent
from payguard.models import FindingCode, FulfillmentStatus, PaymentStatus, Purchase, fingerprint
from payguard.purchase import purchase
from tests.conftest import FakeSeller

P = PaymentStatus
FP = fingerprint("report", {"ticker": "ACME"})


def drive(repo, row, *states, amount=500_000):
    cur = row.payment_status
    for st in states:
        fields = {}
        if st == P.QUOTED:
            fields = {"quoted_amount_minor": amount}
        if st == P.AUTHORIZED:
            fields = {"authorized_amount_minor": amount}
        if st == P.PAYLOAD_SIGNED:
            fields = {"valid_before": int(time.time()) + 300}
        assert repo.update_status(row.purchase_id, cur, st, **fields), f"{cur}->{st}"
        cur = st
    return repo.get_purchase(row.purchase_id)


def seed(repo, status, fulfillment=FulfillmentStatus.PENDING, intent="i1", fp=FP, service="report", attempt=1, age_s=0):
    row = repo.create_purchase(Purchase.new(principal_id="test", task_id=None, purchase_intent_id=intent, request_fingerprint=fp,
                                            service_id=service, args={"ticker": "ACME"}, network="eip155:84532", attempt=attempt))
    path = {
        P.INTENT_RECORDED: (), P.QUOTED: (P.QUOTED,), P.AUTHORIZED: (P.QUOTED, P.AUTHORIZED),
        P.PAYLOAD_SIGNED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED), P.SENT: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT),
        P.SETTLED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLED),
        P.SETTLEMENT_UNKNOWN: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN),
        P.PAYMENT_FAILED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN, P.PAYMENT_FAILED),
        P.REJECTED: (P.QUOTED, P.REJECTED), P.ABANDONED: (P.ABANDONED,),
    }[status]
    row = drive(repo, row, *path)
    if fulfillment != FulfillmentStatus.PENDING:
        repo.set_fulfillment(row.purchase_id, fulfillment)
    if age_s:
        repo.conn.execute("UPDATE purchases SET created_at = ?, updated_at = ? WHERE purchase_id = ?",
                          (time.time() - age_s, time.time() - age_s, row.purchase_id))
    return repo.get_purchase(row.purchase_id)


def svc(deps):
    return deps.registry["report"]


def test_new_key_proceeds(deps):
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "proceed" and d.attempt == 1 and d.supersedes is None


@pytest.mark.parametrize("status,fulfillment", [(P.SETTLED, FulfillmentStatus.VERIFIED), (P.REJECTED, FulfillmentStatus.PENDING)])
def test_same_key_terminal_rows_return_unchanged(deps, status, fulfillment):
    row = seed(deps.repo, status, fulfillment)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id
    assert deps.repo.findings_for_purchase(row.purchase_id) == []


def test_same_key_in_flight_returns_in_progress_with_duplicate_finding(deps):
    # PRD F7 / design §0 S1: in-flight repeats get DUPLICATE_SUSPECTED too; the response still shows IN_PROGRESS.
    row = seed(deps.repo, P.SENT)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id and not d.reconcile_first
    assert d.row.payment_status == P.SENT
    assert [f.code for f in deps.repo.findings_for_purchase(row.purchase_id)] == ["DUPLICATE_SUSPECTED"]
    from payguard.response import build_response
    assert build_response(deps, d.row)["payment_status"] == "IN_PROGRESS"


def test_same_key_unknown_asks_for_reconcile_then_marks_duplicate(deps):
    row = seed(deps.repo, P.SETTLEMENT_UNKNOWN)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.reconcile_first and deps.repo.findings_for_purchase(row.purchase_id) == []
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1", after_reconcile=True)
    assert d.action == "return" and not d.reconcile_first
    f = deps.repo.findings_for_purchase(row.purchase_id)
    assert [x.code for x in f] == ["DUPLICATE_SUSPECTED"] and f[0].severity == "suspected"
    assert f[0].safe_next_actions == ["human_review", "report_to_seller"]
    # a second hit appends evidence but not a second finding (S1)
    resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1", after_reconcile=True)
    assert [x.code for x in deps.repo.findings_for_purchase(row.purchase_id)] == ["DUPLICATE_SUSPECTED"]
    assert [e.kind for e in deps.repo.list_evidence(row.purchase_id)].count("retry_blocked") == 2


def test_same_key_settled_not_verified_is_duplicate(deps):
    row = seed(deps.repo, P.SETTLED, FulfillmentStatus.NO_DELIVERY)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id
    assert deps.repo.has_finding(row.purchase_id, FindingCode.DUPLICATE_SUSPECTED)


@pytest.mark.parametrize("status", [P.PAYMENT_FAILED, P.ABANDONED])
def test_same_key_proven_failure_creates_next_attempt(deps, status):
    row = seed(deps.repo, status)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "proceed" and d.attempt == 2 and d.supersedes == row.purchase_id


def test_new_intent_same_fingerprint_recent_unverified_is_duplicate(deps):
    row = seed(deps.repo, P.SETTLED, FulfillmentStatus.FAILED, intent="old")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id
    f = deps.repo.findings_for_purchase(row.purchase_id)
    assert [x.code for x in f] == ["DUPLICATE_SUSPECTED"]
    ev = [e for e in deps.repo.list_evidence(row.purchase_id) if e.kind == "retry_blocked"]
    assert ev[-1].payload["variant"] == "new_intent" and ev[-1].payload["purchase_intent_id"] == "new"


def test_new_intent_same_fingerprint_unknown_reconciles_first(deps):
    old = seed(deps.repo, P.SETTLEMENT_UNKNOWN, intent="old")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "return" and d.reconcile_first
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new", after_reconcile=True)
    assert d.action == "return" and not d.reconcile_first and d.row.purchase_id == old.purchase_id
    assert deps.repo.has_finding(old.purchase_id, FindingCode.DUPLICATE_SUSPECTED)


def test_new_intent_verified_pays_again_by_design(deps):
    seed(deps.repo, P.SETTLED, FulfillmentStatus.VERIFIED, intent="old")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "proceed"


def test_duplicate_guard_window_is_15_minutes(deps):
    seed(deps.repo, P.SETTLED, FulfillmentStatus.FAILED, intent="old", age_s=16 * 60)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "proceed"


def test_duplicate_guard_window_14_minutes_still_duplicate(deps):
    row = seed(deps.repo, P.SETTLED, FulfillmentStatus.FAILED, intent="old", age_s=14 * 60)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id


@pytest.mark.parametrize("old_status,other_status,other_fulfillment", [
    (P.ABANDONED, P.SENT, FulfillmentStatus.PENDING),
    (P.PAYMENT_FAILED, P.SETTLED, FulfillmentStatus.NO_DELIVERY),
])
def test_attempt_plus_one_still_hits_duplicate_guard(deps, old_status, other_status, other_fulfillment):
    seed(deps.repo, old_status, intent="i1")
    other = seed(deps.repo, other_status, other_fulfillment, intent="i2")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == other.purchase_id
    assert deps.repo.has_finding(other.purchase_id, FindingCode.DUPLICATE_SUSPECTED)


def test_repeat_under_active_quarantine_reuses_rejected_row(deps):
    bad = seed(deps.repo, P.SETTLED, FulfillmentStatus.NO_DELIVERY, intent="old", fp="x" * 64, service="research")
    deps.repo.add_finding(bad.purchase_id, FindingCode.PAID_NO_DELIVERY, "proven", "no artifact", [], ["human_review"])
    fp2 = fingerprint("research", {"topic": "reworded"})
    d1 = resolve_intent(deps, deps.registry["research"], {"topic": "reworded"}, fp2, "new")
    d2 = resolve_intent(deps, deps.registry["research"], {"topic": "reworded"}, fp2, "new")
    assert d1.row.purchase_id == d2.row.purchase_id and d2.action == "return" and d2.row.attempt == 1
    assert [f.code for f in deps.repo.findings_for_purchase(d1.row.purchase_id)] == ["SERVICE_QUARANTINED"]
    kinds = [e.payload.get("variant") for e in deps.repo.list_evidence(d1.row.purchase_id) if e.kind == "retry_blocked"]
    assert kinds == ["quarantined", "quarantined_repeat"]


def test_service_quarantine_rejects_new_purchase(deps):
    bad = seed(deps.repo, P.SETTLED, FulfillmentStatus.NO_DELIVERY, intent="old", fp="x" * 64, service="research")
    deps.repo.add_finding(bad.purchase_id, FindingCode.PAID_NO_DELIVERY, "proven", "no artifact", [], ["human_review"])
    d = resolve_intent(deps, deps.registry["research"], {"topic": "reworded"}, fingerprint("research", {"topic": "reworded"}), "new")
    assert d.action == "return" and d.row.payment_status == P.REJECTED and d.row.reject_reason == "SERVICE_QUARANTINED"
    f = deps.repo.findings_for_purchase(d.row.purchase_id)
    assert [x.code for x in f] == ["SERVICE_QUARANTINED"] and f[0].safe_next_actions == ["human_review"]
    assert f[0].evidence_ids and deps.repo.list_evidence(d.row.purchase_id)[0].kind == "retry_blocked"  # evidence on its own row
    # the SAME topic under a new intent id is a duplicate hit on the existing record, not a quarantine row
    same_topic_fp = "x" * 64
    d2 = resolve_intent(deps, deps.registry["research"], {"topic": "same"}, same_topic_fp, "new-b")
    assert d2.row.purchase_id == bad.purchase_id and deps.repo.has_finding(bad.purchase_id, FindingCode.DUPLICATE_SUSPECTED)
    # other services are unaffected
    assert resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new2").action == "proceed"
    # resolving the finding lifts the quarantine, also for the key that was quarantined (attempt+1)
    deps.repo.resolve_service_findings("test", "research", FindingCode.PAID_NO_DELIVERY)
    assert resolve_intent(deps, deps.registry["research"], {"topic": "again"}, fingerprint("research", {"topic": "again"}), "new3").action == "proceed"
    d3 = resolve_intent(deps, deps.registry["research"], {"topic": "reworded"}, fingerprint("research", {"topic": "reworded"}), "new")
    assert d3.action == "proceed" and d3.attempt == 2 and d3.supersedes == d.row.purchase_id


def signatures(seller):
    return {q.headers["PAYMENT-SIGNATURE"] for q in seller.paid}


async def test_flow_same_intent_and_new_intent_both_blocked_without_payment(make_deps):
    # SETTLEMENT_UNKNOWN row. With the M2 reconcile stub nothing is resent; with M5's real reconcile and the conftest
    # `never_used` reader, each reconcile resends the IDENTICAL header once. Either way only ONE authorization is ever signed.
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller)
    first = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    assert first["payment_status"] == "SETTLEMENT_UNKNOWN" and len(signatures(seller)) == 1
    same = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    assert same["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in same["findings"]]
    new = await purchase(deps, "report", {"ticker": "ACME"}, "i2")
    assert new["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in new["findings"]]
    assert len(signatures(seller)) == 1  # never a second signed authorization
    assert set(new["safe_next_actions"]) >= {"human_review", "report_to_seller"}
    assert not any("pay" in a or "retry" in a for a in new["safe_next_actions"])
