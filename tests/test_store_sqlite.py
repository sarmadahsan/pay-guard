import time

import pytest

from payguard.models import FulfillmentStatus, PaymentStatus, Purchase, history_entry
from payguard.store.base import DuplicateKeyError
from payguard.store.sqlite import SqliteRepository


@pytest.fixture
def repo(tmp_path):
    return SqliteRepository(tmp_path / "t.db")


def mk(intent="i1", fp="f" * 64, attempt=1, service="report"):
    return Purchase.new(principal_id="p", task_id="t", purchase_intent_id=intent, request_fingerprint=fp,
                        service_id=service, args={"ticker": "ACME"}, network="eip155:84532", attempt=attempt)


def test_wal_and_busy_timeout(repo):
    assert repo.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert repo.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_create_get_and_unique_key(repo):
    row = repo.create_purchase(mk())
    got = repo.get_purchase(row.purchase_id)
    assert got.purchase_id == row.purchase_id and got.args == {"ticker": "ACME"}
    with pytest.raises(DuplicateKeyError):
        repo.create_purchase(mk())
    repo.create_purchase(mk(attempt=2))  # a new attempt is a new key
    assert [r.attempt for r in repo.find_by_key("p", "i1", "f" * 64)] == [1, 2]


def test_update_status_is_compare_and_set(repo):
    row = repo.create_purchase(mk())
    assert repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED,
                              history_entry=history_entry("QUOTED", amount=500000), quoted_amount_minor=500000)
    assert not repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED)  # lost race
    got = repo.get_purchase(row.purchase_id)
    assert got.payment_status == PaymentStatus.QUOTED and got.quoted_amount_minor == 500000
    assert [h["state"] for h in got.history] == ["INTENT_RECORDED", "QUOTED"]
    with pytest.raises(ValueError):
        repo.update_status(row.purchase_id, PaymentStatus.QUOTED, PaymentStatus.SETTLED)  # illegal transition


def test_spent_counts_only_budget_states(repo):
    a = repo.create_purchase(mk(intent="a"))
    b = repo.create_purchase(mk(intent="b"))
    c = repo.create_purchase(mk(intent="c"))
    for r in (a, b, c):
        repo.update_status(r.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, quoted_amount_minor=500000)
        repo.update_status(r.purchase_id, PaymentStatus.QUOTED, PaymentStatus.AUTHORIZED, authorized_amount_minor=500000)
    repo.update_status(b.purchase_id, PaymentStatus.AUTHORIZED, PaymentStatus.REJECTED, reject_reason="SIGNING_FAILED")
    repo.update_status(c.purchase_id, PaymentStatus.AUTHORIZED, PaymentStatus.ABANDONED)
    assert repo.spent_minor("p") == 500000


def test_evidence_and_findings(repo):
    row = repo.create_purchase(mk())
    e1 = repo.add_evidence(row.purchase_id, "quote", {"amount": "500000"})
    f1 = repo.add_finding(row.purchase_id, "PRICE_MISMATCH", "proven", "too expensive", [e1], ["get_purchase"])
    assert [e.kind for e in repo.list_evidence(row.purchase_id)] == ["quote"]
    assert repo.has_finding(row.purchase_id, "PRICE_MISMATCH")
    assert repo.has_unresolved_service_finding("p", "report", "PRICE_MISMATCH")
    listed = repo.list_findings("p", unresolved_only=True)
    assert listed[0]["finding_id"] == f1 and listed[0]["service_id"] == "report"
    assert repo.resolve_findings(row.purchase_id, "PRICE_MISMATCH") == 1
    assert not repo.has_unresolved_service_finding("p", "report", "PRICE_MISMATCH")
    assert repo.list_findings("p", unresolved_only=True) == []
    assert len(repo.list_findings("p", unresolved_only=False)) == 1


def test_list_stuck_and_recent_fingerprint(repo):
    old = repo.create_purchase(mk(intent="old"))
    repo.conn.execute("UPDATE purchases SET updated_at = ?, created_at = ? WHERE purchase_id = ?",
                      (time.time() - 120, time.time() - 120, old.purchase_id))
    fresh = repo.create_purchase(mk(intent="fresh"))
    assert [r.purchase_id for r in repo.list_stuck(60)] == [old.purchase_id]
    recent = repo.find_recent_by_fingerprint("p", "f" * 64, since_ts=time.time() - 60)
    assert [r.purchase_id for r in recent] == [fresh.purchase_id]


def test_reset_clears_principal(repo):
    row = repo.create_purchase(mk())
    repo.add_evidence(row.purchase_id, "quote", {})
    repo.add_finding(row.purchase_id, "NOT_X402", "proven", "x", [], [])
    counts = repo.reset("p")
    assert counts == {"purchases": 1, "evidence": 1, "findings": 1}
    assert repo.get_purchase(row.purchase_id) is None


def test_set_fulfillment(repo):
    row = repo.create_purchase(mk())
    repo.set_fulfillment(row.purchase_id, FulfillmentStatus.VERIFIED, history_entry("VERIFIED"), artifact_hash="abc")
    got = repo.get_purchase(row.purchase_id)
    assert got.fulfillment_status == FulfillmentStatus.VERIFIED and got.artifact_hash == "abc"
