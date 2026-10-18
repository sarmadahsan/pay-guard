import time
from types import SimpleNamespace

import pytest
from hexbytes import HexBytes
from web3 import Web3
from web3.datastructures import AttributeDict

from payguard import chain
from payguard.chain import ChainState
from payguard.purchase import purchase
from payguard.reconcile import reconcile
from payguard.registry import SEPOLIA, USDC
from tests.conftest import FakeSeller

TX = "0x" + "c1" * 32
_PROVEN = object()  # sentinel: an explicit event=None must mean "no event found", not "default to proven"


def st(used, block, ts, event=_PROVEN, tx=None, transfer_ok=None, event_block=None):
    """ChainState factory. used=True without an explicit event defaults to a proven transfer (event 'used', tx, transfer_ok True)."""
    if event is _PROVEN:
        event = None
        if used:
            event, tx, transfer_ok = "used", TX, True
    return ChainState(used=used, block_number=block, block_timestamp=ts, event=event, tx_hash=tx, transfer_ok=transfer_ok,
                      event_block=event_block)


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


async def test_settlement_reports_the_event_block_not_the_head_block(make_deps):
    from payguard.response import build_response

    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(True, 900, int(time.time()), event_block=894)))
    r = await purchase(deps, "research", {"topic": "agents"}, "i1")
    assert r["payment_status"] == "SETTLED"
    ev = [e.payload for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == "settlement"][-1]
    assert ev["block_number"] == 894 and ev["checked_at_block"] == 900
    assert [h for h in deps.repo.get_purchase(r["purchase_id"]).history if h["state"] == "SETTLED"][-1]["block"] == 894
    resp = build_response(deps, deps.repo.get_purchase(r["purchase_id"]))
    assert resp["transactions"][0]["block_number"] == 894


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


# ---- reconcile edge cases (review of 81fced6) -------------------------------------------------------------

def _chain_checks(deps, pid):
    return [e.payload["polls"] for e in deps.repo.list_evidence(pid) if e.kind == "chain_check"]


async def test_resend_nonce_used_does_one_post_resend_read(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "nonce_used"]
    now = int(time.time())
    reader = chain_seq(*([st(False, 1, now)] * 5), st(True, 9, now))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert _chain_checks(deps, r["purchase_id"]) == [5, 1]  # 5 polls before the resend, exactly 1 read after it


async def test_resend_nonce_used_402_does_not_decide_when_chain_still_false(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "nonce_used"]
    reader = chain_seq(st(False, 1, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["settlement_tx"] is None
    assert len(seller.paid) == 2 and len(reader.calls) == 6
    resend = [e for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == "resend"][0]
    assert resend.payload["payment_required_error"] == "invalid_exact_evm_nonce_already_used"  # recorded, not decisive
    assert codes(deps, r["purchase_id"]) == [("SETTLEMENT_UNKNOWN", True)]


async def test_expiry_detected_on_post_resend_read_is_payment_failed(make_deps):
    seller = FakeSeller(mode="timeout")
    now = int(time.time())
    reader = chain_seq(*([st(False, 1, now)] * 5), st(False, 2, now + 10_000))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "PAYMENT_FAILED" and len(seller.paid) == 2
    assert codes(deps, r["purchase_id"]) == [("PAYMENT_FAILED", True)]
    assert deps.repo.spent_minor("test") == 0


async def test_resend_classification_error_leaves_row_unknown(make_deps, monkeypatch):
    import payguard.reconcile as rc

    def boom(*_a, **_k):
        raise RuntimeError("classifier bug")

    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")  # first call: real classifier, row unknown
    monkeypatch.setattr(rc, "classify_outcome", boom)
    seller.paid_modes = ["ok"]
    got = await reconcile(deps, deps.repo.get_purchase(r["purchase_id"]), time.monotonic() + 45)
    assert got.payment_status == "SETTLEMENT_UNKNOWN"
    errs = [e.payload for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == "seller_error"]
    assert any(p.get("stage") == "classify" and p.get("via") == "resend" for p in errs)


async def test_poll_sleep_is_clipped_and_reads_skip_when_budget_is_nearly_gone(make_deps):
    seller = FakeSeller(mode="timeout")
    reader = chain_seq(st(False, 1, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    row = deps.repo.get_purchase(r["purchase_id"])
    slept = []

    async def rec_sleep(s):
        slept.append(s)

    deps.sleep = rec_sleep
    calls_before = len(reader.calls)
    await reconcile(deps, row, time.monotonic() + 1.2)
    assert slept and all(s <= 1.2 for s in slept)  # never sleeps past the call budget
    assert len(seller.paid) == 2  # under RESEND_TIMEOUT_S left: no resend
    n = len(reader.calls)
    assert n > calls_before
    await reconcile(deps, row, time.monotonic() + 0.3)  # under MIN_READ_S left: no read at all
    assert len(reader.calls) == n


# ---- chain.py against a fake web3 (same surface and value types as web3 7 / hexbytes 2) ----------------------

BUYER = Web3.to_checksum_address("0x" + "11" * 20)
PAYEE = Web3.to_checksum_address("0x000000000000000000000000000000000000dead")
OTHER = Web3.to_checksum_address("0x" + "22" * 20)
NONCE = "0x" + "ab" * 32
CHAIN_TX = "0x" + "cd" * 32
HEAD = 10_000


def _t(h: str) -> HexBytes:
    return HexBytes(h)


def _addr_topic(a: str) -> HexBytes:
    return HexBytes(bytes(12) + bytes.fromhex(a[2:]))


def auth_log(kind: str, block: int, tx: str = CHAIN_TX, nonce: str = NONCE, who: str = BUYER) -> AttributeDict:
    topic0 = chain.TOPIC_USED if kind == "used" else chain.TOPIC_CANCELED
    return AttributeDict({"address": Web3.to_checksum_address(USDC[SEPOLIA]), "blockNumber": block,
                          "transactionHash": _t(tx), "topics": [_t(topic0), _addr_topic(who), _t(nonce)],
                          "data": HexBytes(b"")})


def transfer_log(frm: str, to: str, value: int, token: str = USDC[SEPOLIA]) -> AttributeDict:
    return AttributeDict({"address": Web3.to_checksum_address(token),
                          "topics": [_t(chain.TOPIC_TRANSFER), _addr_topic(frm), _addr_topic(to)],
                          "data": HexBytes(value.to_bytes(32, "big"))})


class FakeEth:
    def __init__(self, used: bool, events=(), receipts=None, log_delay_s: float = 0.0):
        self.used = used
        self.log_delay_s = log_delay_s
        self.events = list(events)
        self.receipts = receipts or {}
        self.log_queries = []
        self.state_calls = []

    def get_block(self, ident):
        assert ident == "latest"
        return AttributeDict({"number": HEAD, "timestamp": 1_800_000_000})

    def get_logs(self, flt):
        self.log_queries.append(flt)
        if self.log_delay_s:
            time.sleep(self.log_delay_s)  # runs in the worker thread, like a slow RPC
        assert flt["toBlock"] - flt["fromBlock"] + 1 <= chain.LOG_WINDOW
        topic0s, who, nonce = flt["topics"]
        return [e for e in self.events
                if flt["fromBlock"] <= e["blockNumber"] <= flt["toBlock"]
                and chain._hex(e["topics"][0]) in topic0s and chain._hex(e["topics"][1]) == who
                and chain._hex(e["topics"][2]) == nonce]

    def get_transaction_receipt(self, tx_hash):
        assert isinstance(tx_hash, HexBytes)
        return AttributeDict({"logs": self.receipts[chain._hex(tx_hash)]})

    def contract(self, address, abi):
        eth = self

        class _Call:
            def __init__(self, a, n):
                self.a, self.n = a, n

            def call(self, block_identifier):
                eth.state_calls.append((self.a, self.n, block_identifier))
                return eth.used

        return SimpleNamespace(functions=SimpleNamespace(authorizationState=lambda a, n: _Call(a, n)))


@pytest.fixture
def fake_w3(monkeypatch):
    def install(eth: FakeEth):
        monkeypatch.setattr(chain, "_w3", lambda _net: SimpleNamespace(eth=eth))
        return eth
    return install


def eth_w3(eth):
    return SimpleNamespace(eth=eth)


def _read(value=750_000, pay_to=PAYEE, since_s=60, stop_at=None):
    return chain._read(SEPOLIA, BUYER, NONCE, pay_to, value, time.time() - since_s, stop_at)


def test_chain_used_event_with_matching_transfer_is_proven(fake_w3):
    eth = fake_w3(FakeEth(True, [auth_log("used", HEAD - 20)], {CHAIN_TX: [transfer_log(BUYER, PAYEE, 750_000)]}))
    s = _read()
    assert (s.used, s.event, s.tx_hash, s.transfer_ok) == (True, "used", CHAIN_TX, True)
    assert s.block_number == HEAD and s.block_timestamp == 1_800_000_000
    assert eth.state_calls == [(BUYER, bytes.fromhex(NONCE[2:]), HEAD)]  # state read at the same block as the clock


@pytest.mark.parametrize("log", [transfer_log(BUYER, PAYEE, 750_001), transfer_log(BUYER, OTHER, 750_000),
                                 transfer_log(OTHER, PAYEE, 750_000), transfer_log(BUYER, PAYEE, 750_000, token=OTHER)],
                         ids=["value", "pay_to", "from", "token"])
def test_chain_used_event_with_mismatched_transfer_is_not_ok(fake_w3, log):
    fake_w3(FakeEth(True, [auth_log("used", HEAD - 20)], {CHAIN_TX: [log]}))
    s = _read()
    assert (s.used, s.event, s.tx_hash, s.transfer_ok) == (True, "used", CHAIN_TX, False)


def test_chain_canceled_event(fake_w3):
    fake_w3(FakeEth(True, [auth_log("canceled", HEAD - 20)]))
    s = _read()
    assert (s.event, s.tx_hash, s.transfer_ok) == ("canceled", CHAIN_TX, False)


def test_chain_event_in_second_window(fake_w3):
    # purchase started ~300 blocks ago (2 s blocks): search starts 300 + margin blocks back; the event sits in window 2.
    since_s = 2 * 300 - 1  # ceil((599 + a few ms) / 2) == 300
    start = HEAD - 300 - chain.START_MARGIN_BLOCKS
    eth = fake_w3(FakeEth(True, [auth_log("used", start + chain.LOG_WINDOW + 5)],
                          {CHAIN_TX: [transfer_log(BUYER, PAYEE, 750_000)]}))
    s = _read(since_s=since_s)
    assert (s.event, s.transfer_ok) == ("used", True)
    assert [(q["fromBlock"], q["toBlock"]) for q in eth.log_queries] == [
        (start, start + chain.LOG_WINDOW - 1), (start + chain.LOG_WINDOW, start + 2 * chain.LOG_WINDOW - 1)]
    q = eth.log_queries[0]
    assert q["address"] == Web3.to_checksum_address(USDC[SEPOLIA])
    assert q["topics"] == [[chain.TOPIC_USED, chain.TOPIC_CANCELED], "0x" + "00" * 12 + BUYER[2:].lower(), NONCE]


def test_chain_nothing_found_returns_unproven(fake_w3):
    eth = fake_w3(FakeEth(True, [auth_log("used", HEAD - 20, nonce="0x" + "ee" * 32)]))  # other nonce only
    assert chain._find_event(eth_w3(eth), USDC[SEPOLIA], BUYER, NONCE, PAYEE, 750_000, HEAD - 250, HEAD) == (None, None, None, True, None)
    assert len(eth.log_queries) == 3 and eth.log_queries[-1]["toBlock"] == HEAD  # windows cover exactly the range
    s = _read()
    assert (s.used, s.event, s.tx_hash, s.transfer_ok, s.search_complete) == (True, None, None, None, True)


def test_chain_max_windows_exhausted_is_incomplete(fake_w3, monkeypatch):
    monkeypatch.setattr(chain, "MAX_WINDOWS", 2)
    eth = fake_w3(FakeEth(True))
    assert chain._find_event(eth_w3(eth), USDC[SEPOLIA], BUYER, NONCE, PAYEE, 750_000, HEAD - 250, HEAD) == (None, None, None, False, None)
    assert len(eth.log_queries) == 2


def test_chain_state_false_does_no_log_search(fake_w3):
    eth = fake_w3(FakeEth(False, [auth_log("used", HEAD - 20)]))
    s = _read()
    assert (s.used, s.event) == (False, None) and eth.log_queries == []


def test_chain_log_search_stops_at_stop_time(fake_w3):
    eth = fake_w3(FakeEth(True, [auth_log("used", HEAD - 20)]))
    s = _read(stop_at=time.monotonic() - 1)
    assert (s.used, s.event, s.search_complete) == (True, None, False) and eth.log_queries == []


async def test_authorization_state_bounds_the_thread_by_the_call_deadline(fake_w3):
    eth = fake_w3(FakeEth(True, [auth_log("used", HEAD - 20)], {CHAIN_TX: [transfer_log(BUYER, PAYEE, 750_000)]}))
    token = chain.CALL_DEADLINE.set(time.monotonic() - 1)
    try:
        s = await chain.authorization_state(SEPOLIA, BUYER, NONCE, PAYEE, 750_000, time.time() - 60)
    finally:
        chain.CALL_DEADLINE.reset(token)
    assert (s.event, s.search_complete) == (None, False) and eth.log_queries == []
    s = await chain.authorization_state(SEPOLIA, BUYER, NONCE, PAYEE, 750_000, time.time() - 60)  # default budget
    assert (s.event, s.transfer_ok) == ("used", True)


# ---- reconcile driving the real chain.authorization_state over a fake w3 -------------------------------------

async def _unknown_row_with_real_reader(make_deps, fake_w3, eth_factory):
    """A SETTLEMENT_UNKNOWN row (original send + one resend, chain says false), then switch deps to the real reader."""
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN"
    pp = next(e.payload for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == "payment_payload")
    row = deps.repo.get_purchase(r["purchase_id"])
    eth = fake_w3(eth_factory(pp, row))
    deps.chain_reader = chain.authorization_state
    return deps, seller, row, eth


def _proven_eth(pp, row, **kw):
    tx = "0x" + "ef" * 32
    return FakeEth(True, [auth_log("used", HEAD - 5, tx=tx, nonce=pp["nonce"], who=pp["from"])],
                   {tx: [transfer_log(pp["from"], row.pay_to, int(pp["value"]))]}, **kw)


async def test_poll_passes_the_call_deadline_into_the_real_read(make_deps, fake_w3, monkeypatch):
    deps, _seller, row, _eth = await _unknown_row_with_real_reader(make_deps, fake_w3, _proven_eth)
    seen = []
    real_read = chain._read

    def spy(*a):
        seen.append(a[-1])  # stop_at
        return real_read(*a)

    monkeypatch.setattr(chain, "_read", spy)
    deadline = time.monotonic() + 5  # far below READ_BUDGET_S, so the call deadline must win
    got = await reconcile(deps, row, deadline)
    assert seen == [deadline]  # CALL_DEADLINE reached the worker thread's read
    assert got.payment_status == "SETTLED" and got.settlement_tx == "0x" + "ef" * 32


async def test_deadline_truncated_log_search_is_no_information(make_deps, fake_w3, monkeypatch):
    from payguard.purchase import reconcile_and_finish
    from payguard.response import build_response

    monkeypatch.setattr(chain, "LOG_WINDOW", 30)  # range HEAD-60..HEAD -> 3 windows; the event is in the last one
    monkeypatch.setattr(chain, "READ_BUDGET_S", 0.2)  # the thread's stop time passes during the first slow window
    deps, seller, row, eth = await _unknown_row_with_real_reader(
        make_deps, fake_w3, lambda pp, row: _proven_eth(pp, row, log_delay_s=0.3))
    paid_before = len(seller.paid)
    got = await reconcile_and_finish(deps, row, time.monotonic() + 10)
    assert len(eth.log_queries) == 1  # stopped after the first window: the event was never looked at
    assert got.payment_status == "SETTLEMENT_UNKNOWN" and len(seller.paid) == paid_before  # consumed nonce: no resend
    assert "nonce_consumed_search_incomplete" in [h["state"] for h in got.history]
    assert "nonce_consumed_unproven" not in [h["state"] for h in got.history]
    open_f = [f for f in deps.repo.findings_for_purchase(row.purchase_id) if f.resolved_at is None]
    assert [f.code for f in open_f] == ["SETTLEMENT_UNKNOWN"] and not any("Do not retry" in f.summary for f in open_f)
    check = [e.payload for e in deps.repo.list_evidence(row.purchase_id) if e.kind == "chain_check"][-1]
    assert check["used"] is True and check["search_complete"] is False
    resp = build_response(deps, deps.repo.get_purchase(row.purchase_id))
    assert "reconcile_purchase" in resp["safe_next_actions"]


async def test_resend_that_settles_skips_the_post_resend_poll(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "ok"]
    reader = chain_seq(st(False, 7, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLED" and len(reader.calls) == 5  # no read after the settling resend


async def test_row_moved_out_of_unknown_by_resend_classification_skips_the_post_resend_poll(make_deps, monkeypatch):
    import payguard.reconcile as rc
    from payguard.models import PaymentStatus, history_entry

    seller = FakeSeller(mode="timeout")
    reader = chain_seq(st(False, 1, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    calls_before = len(reader.calls)

    def concurrent_fail(repo, row, *_a, **_k):  # another call proved PAYMENT_FAILED meanwhile
        repo.update_status(row.purchase_id, PaymentStatus.SETTLEMENT_UNKNOWN, PaymentStatus.PAYMENT_FAILED,
                           history_entry("PAYMENT_FAILED", reason="concurrent"))
        return repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(rc, "classify_outcome", concurrent_fail)
    got = await reconcile(deps, deps.repo.get_purchase(r["purchase_id"]), time.monotonic() + 45)
    assert got.payment_status == "PAYMENT_FAILED"
    assert len(reader.calls) == calls_before + 5  # the 5 pre-resend polls only
    assert hist(deps, r["purchase_id"])[-1] == "PAYMENT_FAILED"  # no still_unknown entry after it


# ---- single-flight reconcile per purchase (#74) --------------------------------------------------------------

def gated_reader(state, gate=None, delay_s=0.01):
    """Fake chain reader that yields to the loop before answering (or waits for `gate`). Records calls."""
    import asyncio

    calls = []

    async def reader(network, from_addr, nonce, pay_to, value_minor, since_ts):
        calls.append(nonce)
        if gate is not None:
            await gate.wait()
        else:
            await asyncio.sleep(delay_s)
        return state

    reader.calls = calls
    return reader


async def _unknown_row(make_deps):
    """A SETTLEMENT_UNKNOWN row: original paid send + the first call's single resend, chain says not used."""
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq(st(False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and len(seller.paid) == 2
    return deps, seller, deps.repo.get_purchase(r["purchase_id"])


def _kinds(deps, pid):
    return [e.kind for e in deps.repo.list_evidence(pid)]


async def test_concurrent_reconciles_of_one_row_resend_once(make_deps):
    import asyncio

    import payguard.reconcile as rc

    deps, seller, row = await _unknown_row(make_deps)
    reader = gated_reader(st(False, 2, int(time.time())))
    deps.chain_reader = reader
    seller.paid_modes = ["ok"]  # the (single) resend delivers
    paid_before, kinds_before = len(seller.paid), _kinds(deps, row.purchase_id)
    a, b = await asyncio.gather(reconcile(deps, row, time.monotonic() + 45), reconcile(deps, row, time.monotonic() + 45))
    kinds = _kinds(deps, row.purchase_id)
    assert len(seller.paid) == paid_before + 1  # exactly one resend across both callers
    assert kinds.count("resend") == kinds_before.count("resend") + 1
    assert kinds.count("chain_check") == kinds_before.count("chain_check") + 1
    assert len(reader.calls) == rc.POLL_ATTEMPTS  # only the leader read the chain
    assert a.payment_status == b.payment_status == "SETTLED"
    assert "reconcile_joined" in hist(deps, row.purchase_id)
    assert rc._RUNNING == {}


async def test_concurrent_reconcile_and_finish_settles_and_verifies_once(make_deps):
    import asyncio

    from payguard.purchase import reconcile_and_finish

    import payguard.reconcile as rc

    deps, seller, row = await _unknown_row(make_deps)
    reader = gated_reader(st(True, 9, int(time.time())))
    deps.chain_reader = reader
    paid_before, kinds_before = len(seller.paid), _kinds(deps, row.purchase_id)
    a, b = await asyncio.gather(reconcile_and_finish(deps, row, time.monotonic() + 45),
                                reconcile_and_finish(deps, row, time.monotonic() + 45))
    kinds = _kinds(deps, row.purchase_id)
    assert a.payment_status == b.payment_status == "SETTLED" and a.settlement_tx == b.settlement_tx == TX
    assert a.fulfillment_status == b.fulfillment_status == "NO_DELIVERY"
    assert kinds.count("settlement") == kinds_before.count("settlement") + 1 == 1
    assert kinds.count("chain_check") == kinds_before.count("chain_check") + 1
    assert len(reader.calls) == 1 and len(seller.paid) == paid_before
    assert [c for c, _ in codes(deps, row.purchase_id)].count("PAID_NO_DELIVERY") == 1  # verification ran once
    assert hist(deps, row.purchase_id).count("SETTLED") == 1
    assert rc._RUNNING == {}


async def test_joiner_respects_its_own_deadline_and_never_cancels_the_leader(make_deps):
    import asyncio

    import payguard.reconcile as rc

    deps, seller, row = await _unknown_row(make_deps)
    gate = asyncio.Event()
    reader = gated_reader(st(True, 9, int(time.time())), gate=gate)
    deps.chain_reader = reader
    leader = asyncio.create_task(reconcile(deps, row, time.monotonic() + 45))
    while not reader.calls:  # the leader is now blocked inside its first chain read
        await asyncio.sleep(0)
    t0 = time.monotonic()
    joined = await reconcile(deps, row, time.monotonic() + 0.1)
    assert time.monotonic() - t0 < 1.0
    assert joined.payment_status == "SETTLEMENT_UNKNOWN" and not leader.done()
    assert len(reader.calls) == 1  # the joiner did not read the chain itself
    assert await reconcile(deps, row, time.monotonic() - 1) is not None  # an already-expired joiner does not wait
    gate.set()
    done = await leader
    assert done.payment_status == "SETTLED" and done.settlement_tx == TX
    assert rc._RUNNING == {}


async def test_registry_is_cleared_when_the_leader_raises_or_is_cancelled(make_deps, monkeypatch):
    import asyncio

    import payguard.reconcile as rc

    deps, seller, row = await _unknown_row(make_deps)
    real = rc._reconcile
    started = asyncio.Event()

    async def boom(*_a, **_k):
        started.set()
        await asyncio.sleep(0.01)
        raise RuntimeError("reconcile bug")

    monkeypatch.setattr(rc, "_reconcile", boom)
    leader = asyncio.create_task(reconcile(deps, row, time.monotonic() + 45))
    await started.wait()
    joined = await reconcile(deps, row, time.monotonic() + 45)  # a joiner never sees the leader's exception
    assert joined.payment_status == "SETTLEMENT_UNKNOWN"
    with pytest.raises(RuntimeError, match="reconcile bug"):
        await leader
    assert rc._RUNNING == {}

    async def hang(*_a, **_k):
        await asyncio.Event().wait()

    monkeypatch.setattr(rc, "_reconcile", hang)
    leader = asyncio.create_task(reconcile(deps, row, time.monotonic() + 45))
    await asyncio.sleep(0)
    assert row.purchase_id in rc._RUNNING
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert rc._RUNNING == {}

    monkeypatch.setattr(rc, "_reconcile", real)  # a later reconcile runs normally
    deps.chain_reader = chain_seq(st(True, 9, int(time.time())))
    got = await reconcile(deps, row, time.monotonic() + 45)
    assert got.payment_status == "SETTLED" and rc._RUNNING == {}
