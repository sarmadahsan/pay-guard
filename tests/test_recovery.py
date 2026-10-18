import dataclasses
import time
from types import SimpleNamespace

from payguard import reconcile as reconcile_mod
from payguard import recovery as recovery_mod
from payguard.models import EvidenceKind, FindingCode, PaymentStatus
from payguard.purchase import _ensure_unknown_finding
from payguard.recovery import RECHECK_MARGIN_S, STALE_S, run_startup_recovery
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


async def test_recheck_wait_is_clamped_against_clock_skew(deps, monkeypatch):
    _noop_reconcile(monkeypatch)
    row = seed(deps.repo, P.SENT)
    future = dataclasses.replace(row, updated_at=time.time() + 3600)
    # list_stuck's SQL never returns a future-dated row; force it to model a stepped-back clock
    monkeypatch.setattr(deps.repo, "list_stuck", lambda s: [] if s else [future])
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(deps, "sleep", fake_sleep, raising=False)
    await run_startup_recovery(deps)
    assert len(slept) == 1 and slept[0] <= STALE_S + RECHECK_MARGIN_S


async def test_live_row_started_during_wait_is_left_alone(deps, monkeypatch):
    _noop_reconcile(monkeypatch)
    young = seed(deps.repo, P.SENT, intent="young", age_s=10)
    live = []

    async def fake_sleep(s):
        old = time.time() - STALE_S - 5
        deps.repo.conn.execute("UPDATE purchases SET updated_at = ? WHERE purchase_id = ?", (old, young.purchase_id))
        live.append(seed(deps.repo, P.SENT, intent="live"))

    monkeypatch.setattr(deps, "sleep", fake_sleep, raising=False)
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(live[0].purchase_id).payment_status == P.SENT
    assert deps.repo.get_purchase(young.purchase_id).payment_status == P.SETTLEMENT_UNKNOWN


async def test_finding_failure_after_cas_still_reconciles(deps, monkeypatch):
    # CodeRabbit PR #71: once the row is SETTLEMENT_UNKNOWN, list_stuck no longer returns it, so a failure while
    # adding the finding must not drop it from phase 2 (reconcile_and_finish adds the finding itself).
    calls = []

    async def fake_reconcile(d, row, deadline):
        calls.append(row.purchase_id)
        return d.repo.get_purchase(row.purchase_id)

    def boom(_repo, _row):
        raise RuntimeError("finding write failed")

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    monkeypatch.setattr(recovery_mod, "_ensure_unknown_finding", boom)
    row = seed(deps.repo, P.SENT, age_s=STALE_S + 5)
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(row.purchase_id).payment_status == P.SETTLEMENT_UNKNOWN
    assert calls == [row.purchase_id]
    assert any(f.code == "SETTLEMENT_UNKNOWN" for f in deps.repo.findings_for_purchase(row.purchase_id))


# ---- #76: retry reconcile for SETTLEMENT_UNKNOWN rows left by an earlier run --------------------------------------

UNKNOWN_TX = "0x" + "c7" * 32


def _proven_reader(calls):
    async def reader(network, from_addr, nonce, pay_to, value_minor, since_ts):
        calls.append(nonce)
        return SimpleNamespace(used=True, block_number=900, block_timestamp=int(time.time()), event="used",
                               tx_hash=UNKNOWN_TX, transfer_ok=True, event_block=899, search_complete=True)
    return reader


def _failing_reader(calls):
    async def reader(network, from_addr, nonce, pay_to, value_minor, since_ts):
        calls.append(nonce)
        raise ConnectionError("rpc down")
    return reader


def _backdate(repo, pid, age_s):
    repo.conn.execute("UPDATE purchases SET updated_at = ? WHERE purchase_id = ?", (time.time() - age_s, pid))


def _old_unknown(repo, intent, age_s=STALE_S + 30, finding=True):
    """A SETTLEMENT_UNKNOWN row an earlier run left behind: signed payload evidence, optional open finding."""
    row = seed(repo, P.SETTLEMENT_UNKNOWN, intent=intent)
    repo.add_evidence(row.purchase_id, EvidenceKind.PAYMENT_PAYLOAD, {
        "header": "stored-header", "from": "0x" + "11" * 20, "to": row.pay_to or "0x" + "22" * 20,
        "value": "500000", "nonce": f"0x{intent}", "validAfter": "0", "validBefore": str(int(time.time()) + 3600),
        "network": row.network})
    if finding:
        _ensure_unknown_finding(repo, repo.get_purchase(row.purchase_id))
    _backdate(repo, row.purchase_id, age_s)
    return repo.get_purchase(row.purchase_id)


def _open_unknown_findings(repo, pid):
    return [f for f in repo.findings_for_purchase(pid) if f.code == FindingCode.SETTLEMENT_UNKNOWN and f.resolved_at is None]


async def test_old_unknown_row_with_finding_is_reconciled_at_startup(deps):
    calls = []
    deps.chain_reader = _proven_reader(calls)
    row = _old_unknown(deps.repo, "old")
    assert _open_unknown_findings(deps.repo, row.purchase_id)
    await run_startup_recovery(deps)
    got = deps.repo.get_purchase(row.purchase_id)
    assert got.payment_status == P.SETTLED and got.settlement_tx == UNKNOWN_TX
    assert _open_unknown_findings(deps.repo, row.purchase_id) == []
    assert len(calls) == 1


async def test_old_unknown_row_without_finding_is_found_via_purchase_list(deps):
    calls = []
    deps.chain_reader = _proven_reader(calls)
    row = _old_unknown(deps.repo, "nofinding", finding=False)
    assert deps.repo.findings_for_purchase(row.purchase_id) == []
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(row.purchase_id).payment_status == P.SETTLED
    assert len(calls) == 1


async def test_young_unknown_row_is_left_to_its_live_call(deps):
    calls = []
    deps.chain_reader = _proven_reader(calls)
    row = _old_unknown(deps.repo, "young", age_s=5)
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(row.purchase_id).payment_status == P.SETTLEMENT_UNKNOWN
    assert calls == []


async def test_unknown_pass_is_capped_at_max_rows_oldest_first(deps, caplog):
    calls = []
    deps.chain_reader = _proven_reader(calls)
    n = recovery_mod.UNKNOWN_RETRY_MAX + 2
    rows = [_old_unknown(deps.repo, f"cap{i}", age_s=STALE_S + 100 - i) for i in range(n)]  # cap0 is the oldest
    with caplog.at_level("INFO", logger="payguard.recovery"):
        await run_startup_recovery(deps)
    statuses = [deps.repo.get_purchase(r.purchase_id).payment_status for r in rows]
    assert len(calls) == recovery_mod.UNKNOWN_RETRY_MAX
    assert statuses == [P.SETTLED] * recovery_mod.UNKNOWN_RETRY_MAX + [P.SETTLEMENT_UNKNOWN] * 2
    assert "2 left" in caplog.text


async def test_unknown_pass_stops_when_total_budget_is_spent(deps, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(recovery_mod, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(recovery_mod, "UNKNOWN_RETRY_BUDGET_S", 50.0)
    seen = []

    async def slow(d, row, deadline):
        seen.append((row.purchase_id, deadline))
        clock[0] += 30.0  # each row burns 30 s of the 50 s pass budget
        return row

    monkeypatch.setattr(recovery_mod, "reconcile_and_finish", slow)
    rows = [_old_unknown(deps.repo, f"b{i}", age_s=STALE_S + 100 - i) for i in range(3)]
    await run_startup_recovery(deps)
    assert [pid for pid, _ in seen] == [rows[0].purchase_id, rows[1].purchase_id]
    # first row: full call budget; second row: clipped to what is left of the pass budget
    assert seen[0][1] == 1000.0 + recovery_mod.CALL_BUDGET_S and seen[1][1] == 1050.0


async def test_unknown_pass_continues_after_a_row_raises(deps, monkeypatch, caplog):
    seen = []

    async def flaky(d, row, deadline):
        seen.append(row.purchase_id)
        if len(seen) == 1:
            raise RuntimeError("boom")
        return row

    monkeypatch.setattr(recovery_mod, "reconcile_and_finish", flaky)
    a = _old_unknown(deps.repo, "ra", age_s=STALE_S + 50)
    b = _old_unknown(deps.repo, "rb", age_s=STALE_S + 40)
    with caplog.at_level("INFO", logger="payguard.recovery"):
        await run_startup_recovery(deps)
    assert seen == [a.purchase_id, b.purchase_id]
    assert "boom" in caplog.text


async def test_candidate_gathering_failure_is_logged_not_fatal(deps, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(deps.repo, "list_findings", broken)
    with caplog.at_level("INFO", logger="payguard.recovery"):
        await run_startup_recovery(deps)
    assert "store down" in caplog.text


async def test_row_moved_to_unknown_by_phase1_is_reconciled_once(deps, monkeypatch):
    seen = []

    async def counting(d, row, deadline):
        seen.append(row.purchase_id)
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(recovery_mod, "reconcile_and_finish", counting)
    row = seed(deps.repo, P.SENT, intent="p1", age_s=STALE_S + 5)
    real_update = deps.repo.update_status

    def update_keeping_age(pid, *a, **k):  # keep the row "old" so only the phase-2 exclusion prevents a 2nd reconcile
        ok = real_update(pid, *a, **k)
        _backdate(deps.repo, pid, STALE_S + 5)
        return ok

    monkeypatch.setattr(deps.repo, "update_status", update_keeping_age)
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(row.purchase_id).payment_status == P.SETTLEMENT_UNKNOWN
    assert seen == [row.purchase_id]


async def test_unknown_pass_makes_no_status_write_of_its_own(deps, monkeypatch):
    calls = []
    deps.chain_reader = _failing_reader(calls)
    row = _old_unknown(deps.repo, "nowrite")
    before = deps.repo.get_purchase(row.purchase_id).history
    writes = []
    real = deps.repo.update_status

    def spy(*a, **k):
        writes.append(a)
        return real(*a, **k)

    monkeypatch.setattr(deps.repo, "update_status", spy)
    await run_startup_recovery(deps)
    got = deps.repo.get_purchase(row.purchase_id)
    assert calls  # the pass did run reconcile (its chain read failed: no information)
    assert writes == [] and got.payment_status == P.SETTLEMENT_UNKNOWN and got.history == before
    assert len(_open_unknown_findings(deps.repo, row.purchase_id)) == 1


async def test_settled_old_rows_are_not_candidates(deps, caplog):
    seed(deps.repo, P.SETTLED, intent="done", age_s=STALE_S + 5)
    with caplog.at_level("INFO", logger="payguard.recovery"):
        await run_startup_recovery(deps)
    assert "startup recovery: nothing to do" in caplog.text


async def test_unknown_rows_without_payment_payload_do_not_use_retry_slots(deps):
    calls = []
    deps.chain_reader = _proven_reader(calls)
    # older rows reconcile can never act on (no signed payload stored): they must not crowd out the real one
    for i in range(recovery_mod.UNKNOWN_RETRY_MAX):
        r = seed(deps.repo, P.SETTLEMENT_UNKNOWN, intent=f"nopayload{i}")
        _ensure_unknown_finding(deps.repo, deps.repo.get_purchase(r.purchase_id))
        _backdate(deps.repo, r.purchase_id, STALE_S + 500 - i)
    row = _old_unknown(deps.repo, "haspayload")
    await run_startup_recovery(deps)
    assert deps.repo.get_purchase(row.purchase_id).payment_status == P.SETTLED
    assert len(calls) == 1
