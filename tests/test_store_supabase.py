"""Supabase repository tests.

Integration tests run only when SUPABASE_URL and SUPABASE_SERVICE_KEY are set. Uses a throwaway principal id per run.
Unit tests at the bottom use a tiny fake query builder and need no network.
"""
import os
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from postgrest.exceptions import APIError

from payguard.models import FulfillmentStatus, PaymentStatus, Purchase, history_entry
from payguard.store.base import DuplicateKeyError

needs_supabase = pytest.mark.skipif(not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")),
                                    reason="SUPABASE_URL/SUPABASE_SERVICE_KEY not set")


@pytest.fixture
def repo():
    from payguard.store.supabase import SupabaseRepository

    return SupabaseRepository.from_env()


@pytest.fixture
def principal(repo):
    pid = f"test-{uuid.uuid4().hex[:8]}"
    yield pid
    repo.reset(pid)


def mk(principal, intent="i1", attempt=1):
    return Purchase.new(principal_id=principal, task_id=None, purchase_intent_id=intent, request_fingerprint="f" * 64,
                        service_id="report", args={"ticker": "ACME"}, network="eip155:84532", attempt=attempt)


@needs_supabase
def test_create_cas_spent_findings_reset(repo, principal):
    row = repo.create_purchase(mk(principal))
    with pytest.raises(DuplicateKeyError):
        repo.create_purchase(mk(principal))
    assert repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED,
                              history_entry("QUOTED"), quoted_amount_minor=500000)
    assert not repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED)
    assert repo.update_status(row.purchase_id, PaymentStatus.QUOTED, PaymentStatus.AUTHORIZED, authorized_amount_minor=500000)
    got = repo.get_purchase(row.purchase_id)
    assert got.payment_status == PaymentStatus.AUTHORIZED and [h["state"] for h in got.history] == ["INTENT_RECORDED", "QUOTED"]
    assert repo.spent_minor(principal) == 500000
    assert type(repo.spent_minor(principal)) is int
    e = repo.add_evidence(row.purchase_id, "quote", {"amount": "500000"})
    repo.add_finding(row.purchase_id, "PRICE_MISMATCH", "proven", "x", [e], ["get_purchase"])
    assert repo.has_unresolved_service_finding(principal, "report", "PRICE_MISMATCH")
    assert repo.list_findings(principal)[0]["service_id"] == "report"
    assert repo.resolve_findings(row.purchase_id, "PRICE_MISMATCH") == 1
    repo.set_fulfillment(row.purchase_id, FulfillmentStatus.VERIFIED, history_entry("VERIFIED"))
    assert repo.get_purchase(row.purchase_id).fulfillment_status == FulfillmentStatus.VERIFIED
    assert [r.attempt for r in repo.find_by_key(principal, "i1", "f" * 64)] == [1]
    assert repo.find_recent_by_fingerprint(principal, "f" * 64, time.time() - 60)[0].purchase_id == row.purchase_id
    assert [r.purchase_id for r in repo.list_purchases(principal, limit=5)] == [row.purchase_id]
    assert repo.reset(principal) == {"purchases": 1, "evidence": 1, "findings": 1}


@needs_supabase
def test_history_survives_interleaved_writers(repo, principal):
    """A history writer that read a stale row must retry, not overwrite another writer's entry."""
    from payguard.store.supabase import SupabaseRepository

    row = repo.create_purchase(mk(principal))
    other = SupabaseRepository.from_env()  # a second process starting side by side
    real_get = repo._get_raw
    raced = []

    def get_then_race(pid, cols="*"):
        cur = real_get(pid, cols)
        if not raced:  # between our read and our write, another writer appends an entry
            raced.append(True)
            other.set_fulfillment(pid, FulfillmentStatus.VERIFIED, history_entry("VERIFIED"))
        return cur

    repo._get_raw = get_then_race
    assert repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, history_entry("QUOTED"))
    states = [h["state"] for h in repo.get_purchase(row.purchase_id).history]
    assert states == ["INTENT_RECORDED", "VERIFIED", "QUOTED"]


@needs_supabase
def test_rejects_protected_and_unknown_columns_live(repo, principal):
    row = repo.create_purchase(mk(principal))
    with pytest.raises(ValueError):
        repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, history=[])
    with pytest.raises(ValueError):
        repo.set_fulfillment(row.purchase_id, FulfillmentStatus.VERIFIED, authorized_amount_minor=1)
    assert repo.get_purchase(row.purchase_id).payment_status == PaymentStatus.INTENT_RECORDED


# ---- unit tests (no network) ------------------------------------------------

class FakeQuery:
    """Records the builder chain; every builder method returns self; execute() pops the next scripted response."""

    def __init__(self, client: "FakeClient", table: str) -> None:
        self.client = client
        self.ops: list[tuple[Any, ...]] = [("table", table)]

    def __getattr__(self, op: str):
        def method(*args: Any, **kwargs: Any) -> "FakeQuery":
            self.ops.append((op, args, kwargs))
            return self
        return method

    def execute(self) -> SimpleNamespace:
        self.client.calls.append(self.ops)
        r = self.client.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(data=r)


class FakeClient:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[list[tuple[Any, ...]]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)


def fake_repo(*responses: Any):
    from payguard.store.supabase import SupabaseRepository

    client = FakeClient(*responses)
    return SupabaseRepository(client), client  # type: ignore[arg-type]


def db_row(p: Purchase, **over: Any) -> dict[str, Any]:
    return {**p.to_dict(), "seq": 1, **over}


@pytest.mark.parametrize("fields", [{"history": []}, {"principal_id": "x"}, {"no_such_col": 1}])
def test_update_status_rejects_bad_columns_before_network(fields):
    repo, client = fake_repo()
    with pytest.raises(ValueError):
        repo.update_status("p1", PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, **fields)
    assert client.calls == []


@pytest.mark.parametrize("fields", [{"authorized_amount_minor": 1}, {"fulfillment_status": "VERIFIED"}, {"bogus": 1}])
def test_set_fulfillment_rejects_bad_columns_before_network(fields):
    repo, client = fake_repo()
    with pytest.raises(ValueError):
        repo.set_fulfillment("p1", FulfillmentStatus.VERIFIED, **fields)
    assert client.calls == []


def test_dict_value_for_plain_column_is_type_error():
    repo, client = fake_repo()
    with pytest.raises(TypeError):
        repo.update_status("p1", PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, reject_reason={"a": 1})
    assert client.calls == []


def test_spent_minor_returns_int_and_paginates():
    repo, client = fake_repo([{"authorized_amount_minor": 500000, "seq": 1}, {"authorized_amount_minor": None, "seq": 2}],
                             [{"authorized_amount_minor": 250000.0, "seq": 3}], [])
    spent = repo.spent_minor("alice")
    assert spent == 750000 and type(spent) is int
    assert len(client.calls) == 3


def test_create_purchase_duplicate_key_on_empty_upsert():
    repo, client = fake_repo([])
    with pytest.raises(DuplicateKeyError):
        repo.create_purchase(mk("alice"))
    ops = client.calls[0]
    upsert = next(o for o in ops if o[0] == "upsert")
    assert upsert[2] == {"on_conflict": "principal_id,purchase_intent_id,request_fingerprint,attempt",
                         "ignore_duplicates": True}


def test_create_purchase_maps_unique_violation_and_reraises_others():
    repo, _ = fake_repo(APIError({"code": "23505", "message": "duplicate key"}),
                        APIError({"code": "42P01", "message": "relation does not exist"}))
    with pytest.raises(DuplicateKeyError):
        repo.create_purchase(mk("alice"))
    with pytest.raises(APIError):
        repo.create_purchase(mk("alice"))


def test_create_purchase_restores_types_and_ignores_extra_columns():
    p = mk("alice")
    repo, _ = fake_repo([db_row(p, quoted_amount_minor=500000.0)])
    got = repo.create_purchase(p)
    assert got.payment_status is PaymentStatus.INTENT_RECORDED
    assert got.fulfillment_status is FulfillmentStatus.PENDING
    assert type(got.quoted_amount_minor) is int and got.purchase_id == p.purchase_id


def test_update_status_without_history_is_a_single_cas_update():
    repo, client = fake_repo([{"purchase_id": "p1"}])
    assert repo.update_status("p1", PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, quoted_amount_minor=5)
    (ops,) = client.calls
    assert ("eq", ("payment_status", "INTENT_RECORDED"), {}) in ops
    body = next(o for o in ops if o[0] == "update")[1][0]
    assert body["payment_status"] == "QUOTED" and body["quoted_amount_minor"] == 5


def test_update_status_history_retry_returns_false_when_status_moved():
    stale = {"history": [{"state": "INTENT_RECORDED"}], "updated_at": 1.0, "payment_status": "INTENT_RECORDED"}
    moved = {"history": [{"state": "INTENT_RECORDED"}, {"state": "ABANDONED"}], "updated_at": 2.0,
             "payment_status": "ABANDONED"}
    # read -> conditional write loses (0 rows) -> re-read shows another writer moved the status
    repo, client = fake_repo([stale], [], [moved])
    assert not repo.update_status("p1", PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, history_entry("QUOTED"))
    write = client.calls[1]
    assert ("eq", ("updated_at", 1.0), {}) in write and ("eq", ("payment_status", "INTENT_RECORDED"), {}) in write
    body = next(o for o in write if o[0] == "update")[1][0]
    assert [h["state"] for h in body["history"]] == ["INTENT_RECORDED", "QUOTED"] and body["updated_at"] != 1.0


def test_history_write_retries_then_succeeds_on_fresh_row():
    first = {"history": [{"state": "A"}], "updated_at": 1.0, "payment_status": "SETTLED"}
    second = {"history": [{"state": "A"}, {"state": "B"}], "updated_at": 2.0, "payment_status": "SETTLED"}
    repo, client = fake_repo([first], [], [second], [{"purchase_id": "p1"}])
    repo.append_history("p1", {"state": "C"})
    body = next(o for o in client.calls[3] if o[0] == "update")[1][0]
    assert [h["state"] for h in body["history"]] == ["A", "B", "C"]


def test_history_write_raises_when_retries_exhausted():
    from payguard.store.supabase import HISTORY_WRITE_ATTEMPTS

    cur = {"history": [], "updated_at": 1.0, "payment_status": "SETTLED"}
    repo, _ = fake_repo(*([[cur], []] * HISTORY_WRITE_ATTEMPTS))
    with pytest.raises(RuntimeError, match="could not write history"):
        repo.set_fulfillment("p1", FulfillmentStatus.VERIFIED, history_entry("VERIFIED"))


def test_add_evidence_coerces_non_json_values():
    repo, client = fake_repo([{}])
    repo.add_evidence("p1", "quote", {"when": object, "n": 1})
    body = next(o for o in client.calls[0] if o[0] == "insert")[1][0]
    assert body["payload"]["n"] == 1 and isinstance(body["payload"]["when"], str)


def test_from_env_names_missing_variable(monkeypatch):
    from payguard.store.supabase import SupabaseRepository

    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_KEY"):
        SupabaseRepository.from_env()


def test_postgrest_wire_format_of_queries_we_rely_on():
    """Builds real supabase-py queries (no .execute(), so no network) mirroring the repository's calls."""
    from urllib.parse import unquote

    from supabase import create_client

    from payguard.store.supabase import FINDING_JOIN, PURCHASE_KEY

    client = create_client("https://x.invalid", "k")
    up = client.table("purchases").upsert({"purchase_id": "p1"}, on_conflict=PURCHASE_KEY, ignore_duplicates=True).request
    assert up.params["on_conflict"] == "principal_id,purchase_intent_id,request_fingerprint,attempt"
    prefer = up.headers["prefer"]
    assert "resolution=ignore-duplicates" in prefer and "return=representation" in prefer

    t = time.time()
    upd = client.table("purchases").update({"x": 1}).eq("purchase_id", "p1").eq("updated_at", t).request
    assert upd.params["updated_at"] == f"eq.{t!r}" and float(upd.params["updated_at"][3:]) == t

    sel = client.table("findings").select(FINDING_JOIN).eq("purchases.principal_id", "p").request
    assert unquote(sel.params["select"]).startswith("*,purchases!inner(principal_id,service_id,")
    assert sel.params["purchases.principal_id"] == "eq.p"


@pytest.mark.parametrize("code", ["42P01", "PGRST205"])
def test_reset_tolerates_missing_wallet_audit_table(code):
    # _purchase_ids page (empty) -> purchases delete -> wallet_audit delete fails: schema not applied
    repo, client = fake_repo([], [], APIError({"code": code, "message": "relation wallet_audit does not exist"}))
    assert repo.reset("alice") == {"purchases": 0, "evidence": 0, "findings": 0}
    assert ("table", "wallet_audit") in client.calls[-1]


def test_reset_propagates_other_wallet_audit_errors():
    """A failed delete on an existing table must not leave a stale wallet baseline behind silently (#88 review)."""
    repo, _ = fake_repo([], [], APIError({"code": "42501", "message": "permission denied"}))
    with pytest.raises(APIError):
        repo.reset("alice")
