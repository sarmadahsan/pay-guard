import time

from payguard import reconcile as reconcile_mod
from payguard.models import PaymentStatus
from payguard.recovery import STALE_S, run_startup_recovery
from tests.test_guard import seed

P = PaymentStatus


async def test_recovery_abandons_unsigned_and_reconciles_signed(deps, monkeypatch):
    calls = []

    async def fake_reconcile(d, row, deadline):
        calls.append(row.purchase_id)
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    rows = {st: seed(deps.repo, st, intent=f"i-{st}", age_s=STALE_S + 5)
            for st in (P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT)}
    fresh = seed(deps.repo, P.SENT, intent="fresh")  # updated just now: must be left alone
    terminal = seed(deps.repo, P.SETTLED, intent="done", age_s=STALE_S + 5)

    await run_startup_recovery(deps)

    for st in (P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED):
        assert deps.repo.get_purchase(rows[st].purchase_id).payment_status == P.ABANDONED
    for st in (P.PAYLOAD_SIGNED, P.SENT):
        got = deps.repo.get_purchase(rows[st].purchase_id)
        assert got.payment_status == P.SETTLEMENT_UNKNOWN and got.history[-1]["by"] == "startup_recovery"
        assert got.purchase_id in calls
        assert any(f.code == "SETTLEMENT_UNKNOWN" for f in deps.repo.findings_for_purchase(got.purchase_id))
    assert deps.repo.get_purchase(fresh.purchase_id).payment_status == P.SENT
    assert deps.repo.get_purchase(terminal.purchase_id).payment_status == P.SETTLED
    # ABANDONED frees the budget; SETTLEMENT_UNKNOWN and SETTLED still count
    assert deps.repo.spent_minor("test") == 500_000 * 4  # two stale signed rows (now unknown), fresh SENT, terminal SETTLED


async def test_recovery_is_idempotent(deps, monkeypatch):
    async def fake_reconcile(d, row, deadline):
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    row = seed(deps.repo, P.QUOTED, age_s=STALE_S + 5)
    await run_startup_recovery(deps)
    await run_startup_recovery(deps)
    got = deps.repo.get_purchase(row.purchase_id)
    assert got.payment_status == P.ABANDONED and [h["state"] for h in got.history].count("ABANDONED") == 1


def _noop_reconcile(monkeypatch, seen=None):
    async def fake(d, row, deadline):
        if seen is not None:
            seen.append(row.purchase_id)
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(reconcile_mod, "reconcile", fake)


async def test_reconcile_failure_keeps_finding_and_continues(deps, monkeypatch):
    seen = []

    async def boom(d, row, deadline):
        seen.append(row.purchase_id)
        if len(seen) == 1:
            raise RuntimeError("rpc down")
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(reconcile_mod, "reconcile", boom)
    a = seed(deps.repo, P.SENT, intent="a", age_s=STALE_S + 10)
    b = seed(deps.repo, P.PAYLOAD_SIGNED, intent="b", age_s=STALE_S + 5)
    await run_startup_recovery(deps)
    assert len(seen) == 2
    for r in (a, b):
        got = deps.repo.get_purchase(r.purchase_id)
        assert got.payment_status == P.SETTLEMENT_UNKNOWN
        found = [f for f in deps.repo.findings_for_purchase(r.purchase_id) if f.code == "SETTLEMENT_UNKNOWN"]
        assert len(found) == 1


async def test_failing_row_does_not_abort_pass(deps, monkeypatch):
    _noop_reconcile(monkeypatch)
    bad = seed(deps.repo, P.QUOTED, intent="bad", age_s=STALE_S + 10)
    good = seed(deps.repo, P.QUOTED, intent="good", age_s=STALE_S + 5)
    real = deps.repo.update_status

    def flaky(pid, *a, **k):
        if pid == bad.purchase_id:
            raise RuntimeError("db hiccup")
        return real(pid, *a, **k)

    monkeypatch.setattr(deps.repo, "update_status", flaky)
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(good.purchase_id).payment_status == P.ABANDONED
    assert deps.repo.get_purchase(bad.purchase_id).payment_status == P.QUOTED


async def test_quick_restart_rechecks_young_rows(deps, monkeypatch):
    _noop_reconcile(monkeypatch)
    row = seed(deps.repo, P.SENT, age_s=10)
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        old = time.time() - STALE_S - 5
        deps.repo.conn.execute("UPDATE purchases SET updated_at = ? WHERE purchase_id = ?", (old, row.purchase_id))

    monkeypatch.setattr(deps, "sleep", fake_sleep, raising=False)
    await run_startup_recovery(deps)
    assert len(slept) == 1 and 0 < slept[0] <= STALE_S
    got = deps.repo.get_purchase(row.purchase_id)
    assert got.payment_status == P.SETTLEMENT_UNKNOWN and got.history[-1]["by"] == "startup_recovery"


async def test_lost_cas_skips_reconcile(deps, monkeypatch):
    seen = []
    _noop_reconcile(monkeypatch, seen)
    row = seed(deps.repo, P.SENT, age_s=STALE_S + 5)
    snapshot = deps.repo.list_stuck(STALE_S)
    # another process settles the row after our snapshot
    deps.repo.update_status(row.purchase_id, P.SENT, P.SETTLED)
    monkeypatch.setattr(deps.repo, "list_stuck", lambda _s: snapshot)
    await run_startup_recovery(deps)
    assert seen == [] and deps.repo.get_purchase(row.purchase_id).payment_status == P.SETTLED


async def test_nothing_to_do_logged_without_sleep(deps, caplog):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    deps.sleep = fake_sleep
    with caplog.at_level("INFO", logger="payguard.recovery"):
        await run_startup_recovery(deps)
    assert "startup recovery: nothing to do" in caplog.text and slept == []


async def test_abandoned_history_marks_recovery(deps, monkeypatch):
    _noop_reconcile(monkeypatch)
    row = seed(deps.repo, P.AUTHORIZED, age_s=STALE_S + 5)
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(row.purchase_id).history[-1]["by"] == "startup_recovery"
