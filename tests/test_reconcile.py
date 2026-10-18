import time

import pytest

from payguard.chain import ChainState
from payguard.purchase import purchase
from payguard.reconcile import reconcile
from tests.conftest import FakeSeller

TX = "0x" + "c1" * 32
_PROVEN = object()  # sentinel: an explicit event=None must mean "no event found", not "default to proven"


def st(used, block, ts, event=_PROVEN, tx=None, transfer_ok=None):
    """ChainState factory. used=True without an explicit event defaults to a proven transfer (event 'used', tx, transfer_ok True)."""
    if event is _PROVEN:
        event = None
        if used:
            event, tx, transfer_ok = "used", TX, True
    return ChainState(used=used, block_number=block, block_timestamp=ts, event=event, tx_hash=tx, transfer_ok=transfer_ok)


def chain_seq(*states):
    """Fake deps.chain_reader: returns ChainStates in order, repeating the last one. Records calls."""
    calls = []

    async def reader(network, from_addr, nonce, pay_to, value_minor, since_ts):
        calls.append((network, from_addr, nonce, pay_to, value_minor))
        return states[min(len(calls) - 1, len(states) - 1)]

    reader.calls = calls
    return reader


def hist(deps, pid):
    return [h["state"] for h in deps.repo.get_purchase(pid).history]


def codes(deps, pid):
    return [(f.code, f.resolved_at is None) for f in deps.repo.findings_for_purchase(pid)]


async def test_used_event_with_matching_transfer_settles_and_no_artifact_is_paid_no_delivery(make_deps):
    seller = FakeSeller(mode="timeout")
    reader = chain_seq(st(True, 123, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "agents"}, "i1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "NO_DELIVERY"
    assert r["settlement_tx"] == TX and r["explorer_url"].endswith(TX)  # proven by AuthorizationUsed + Transfer
    assert [c for c, _ in codes(deps, r["purchase_id"])] == ["PAID_NO_DELIVERY"]
    assert hist(deps, r["purchase_id"])[-5:] == ["SENT", "SETTLEMENT_UNKNOWN", "chain_check", "SETTLED", "NO_DELIVERY"]
    assert len(seller.paid) == 1 and len(reader.calls) == 1
    assert reader.calls[0][3] == seller_pay_to(deps, r) and reader.calls[0][4] == 750_000  # reader gets payTo and amount to verify
    assert r["safe_next_actions"] == ["human_review", "report_to_seller"]
    ev = {e.kind: e.payload for e in deps.repo.list_evidence(r["purchase_id"])}
    assert ev["settlement"]["source"] == "chain:AuthorizationUsed+Transfer" and ev["settlement"]["transaction"] == TX
    assert "resend" not in ev


def seller_pay_to(deps, r):
    return deps.repo.get_purchase(r["purchase_id"]).pay_to


async def test_nonce_consumed_without_transfer_proof_stays_unknown_and_never_resends(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(True, 5, int(time.time()), event=None, tx=None, transfer_ok=None)))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["settlement_tx"] is None
    assert len(seller.paid) == 1  # a consumed nonce is never resent
    f = [x for x in r["findings"] if x["code"] == "SETTLEMENT_UNKNOWN"]
    assert f and "consumed" in f[0]["summary"] and "human_review" in f[0]["safe_next_actions"]
    # Deviation from the plan: the plan also asserted "human_review" in r["safe_next_actions"], which needs a
    # response.py change (outside this task's files). That assertion lives in the strict-xfail test below.
    assert not any("pay" in a or "retry" in a for a in r["safe_next_actions"])
    assert deps.repo.spent_minor("test") == 750_000  # still counts as spent
    # a retry is blocked by the guard (M3) because the row is SETTLEMENT_UNKNOWN
    again = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert again["purchase_id"] == r["purchase_id"] and len(seller.paid) == 1


@pytest.mark.xfail(strict=True, reason="response.safe_next_actions does not yet surface human_review for a "
                                       "SETTLEMENT_UNKNOWN row whose open finding asks for it (response.py is not in Task 5.2)")
async def test_nonce_consumed_without_transfer_proof_response_offers_human_review(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(True, 5, int(time.time()), event=None, tx=None, transfer_ok=None)))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert "human_review" in r["safe_next_actions"]


async def test_used_event_with_mismatched_transfer_stays_unknown(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(True, 5, int(time.time()), event="used", tx=TX, transfer_ok=False)))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and len(seller.paid) == 1


async def test_canceled_authorization_is_payment_failed(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(True, 5, int(time.time()), event="canceled", tx=TX, transfer_ok=False)))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "PAYMENT_FAILED" and codes(deps, r["purchase_id"]) == [("PAYMENT_FAILED", True)]
    assert deps.repo.spent_minor("test") == 0 and len(seller.paid) == 1


async def test_expired_by_block_timestamp_is_payment_failed(make_deps):
    seller = FakeSeller(mode="timeout")
    far_future = int(time.time()) + 10_000  # chain time says the authorization window is long over
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 5, far_future)))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "PAYMENT_FAILED"
    assert codes(deps, r["purchase_id"]) == [("PAYMENT_FAILED", True)]
    assert len(seller.paid) == 1  # no resend after a proven expiry
    assert deps.repo.spent_minor("test") == 0  # PAYMENT_FAILED frees the budget
    assert r["safe_next_actions"] == ["get_purchase"]


async def test_still_valid_resends_identical_header_once_and_settles(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "ok"]  # first paid request stalls, the resend delivers
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 7, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "VERIFIED"
    assert len(seller.paid) == 2
    assert seller.paid[0].headers["PAYMENT-SIGNATURE"] == seller.paid[1].headers["PAYMENT-SIGNATURE"]  # identical header
    kinds = [e.kind for e in deps.repo.list_evidence(r["purchase_id"])]
    assert kinds.count("resend") == 1 and "settlement" in kinds


async def test_resend_nonce_used_triggers_one_more_poll_then_settles(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "nonce_used"]
    now = int(time.time())
    reader = chain_seq(*([st(False, 1, now)] * 5), st(True, 9, now))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "NO_DELIVERY" and r["settlement_tx"] == TX
    assert len(seller.paid) == 2  # exactly one resend
    resend = [e for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == "resend"][0]
    assert resend.payload["payment_required_error"] == "invalid_exact_evm_nonce_already_used"
    assert len(reader.calls) == 6  # 5 polls, then 1 re-poll that returned a proven transfer


async def test_resend_bound_is_one_per_call_and_row_stays_unknown(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["valid_before"] is not None
    assert len(seller.paid) == 2  # original + one resend, never more
    assert codes(deps, r["purchase_id"]) == [("SETTLEMENT_UNKNOWN", True)]
    assert "reconcile_purchase" in r["safe_next_actions"]
    row = deps.repo.get_purchase(r["purchase_id"])
    await reconcile(deps, row, time.monotonic() + 45)  # a later reconcile call gets its own single resend
    assert len(seller.paid) == 3


async def test_deadline_exhausted_means_no_resend(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    row = deps.repo.get_purchase(r["purchase_id"])
    before = len(seller.paid)
    got = await reconcile(deps, row, time.monotonic() - 1)  # already past the budget
    assert got.payment_status == "SETTLEMENT_UNKNOWN" and len(seller.paid) == before


async def test_chain_reader_error_leaves_row_unknown_without_resend(make_deps):
    seller = FakeSeller(mode="timeout")

    async def broken(*_a):
        raise ConnectionError("rpc down")

    deps = make_deps(seller, chain_reader=broken)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and len(seller.paid) == 1
    assert "reconcile_purchase" in r["safe_next_actions"]


async def test_reconcile_is_a_noop_on_non_unknown_rows(deps, seller):
    r = await purchase(deps, "report", {"ticker": "A"}, "i1")
    row = deps.repo.get_purchase(r["purchase_id"])
    got = await reconcile(deps, row, time.monotonic() + 45)
    assert got.payment_status == "SETTLED" and len(seller.paid) == 1
