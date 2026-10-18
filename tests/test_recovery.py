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
