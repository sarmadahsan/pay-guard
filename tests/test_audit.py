"""Wallet audit (issue #87): baseline, cheap balance check, transfer matching, findings. Fake readers, no network."""
from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace
from typing import Any

import pytest
from hexbytes import HexBytes
from web3 import Web3

from payguard import audit, chain
from payguard.chain import Transfer, TransferScan, WalletBalance
from payguard.models import EvidenceKind, FindingCode, PaymentStatus
from payguard.purchase import purchase
from payguard.registry import SEPOLIA, USDC
from payguard.response import build_response, compact_purchase
from tests.conftest import PAY_TO, FakeSeller

P = PaymentStatus
START = 8_750_000
OUTSIDE = "0x000000000000000000000000000000000000bEEF"
FAUCET = "0x000000000000000000000000000000000000FA11"


class FakeChain:
    """A wallet on a fake chain. spend()/receive() mine one block each and move the balance."""

    def __init__(self, address: str, balance: int = START, block: int = 1_000) -> None:
        self.address = address
        self.balance = balance
        self.block = block
        self.ts_offset = 0.0  # chain clock = wall clock + offset
        self.out: list[Transfer] = []
        self.inc: list[Transfer] = []
        self.nonces: dict[str, list[str]] = {}
        self.balance_calls = 0
        self.scans: list[tuple[int, int]] = []
        self.fail_balance = False
        self.fail_scan = False

    async def balance_reader(self, network: str, address: str) -> WalletBalance:
        self.balance_calls += 1
        if self.fail_balance:
            raise ConnectionError("rpc down")
        return WalletBalance(self.balance, self.block, int(time.time() + self.ts_offset))

    async def transfer_reader(self, network: str, address: str, from_block: int, to_block: int, *,
                              stop_at: float | None = None) -> TransferScan:
        self.scans.append((from_block, to_block))
        if self.fail_scan:
            raise ConnectionError("rpc down")
        rng = range(from_block, to_block + 1)
        out = [t for t in self.out if t.block_number in rng]
        return TransferScan(out, [t for t in self.inc if t.block_number in rng],
                            {t.tx_hash: self.nonces[t.tx_hash] for t in out if t.tx_hash in self.nonces}, to_block, True)

    def spend(self, to: str, value: int, tx: str, nonce: str | None = None, *, move: bool = True) -> None:
        self.block += 1
        if move:
            self.balance -= value
        self.out.append(Transfer(tx, 0, self.block, self.address, to, value))
        if nonce:
            self.nonces[tx] = [nonce]

    def receive(self, value: int, tx: str = "0x" + "fa" * 32) -> None:
        self.block += 1
        self.balance += value
        self.inc.append(Transfer(tx, 0, self.block, FAUCET, self.address, value))


@pytest.fixture
def fc(account) -> FakeChain:
    return FakeChain(account.address)


def wire(deps, fc: FakeChain):
    return dataclasses.replace(deps, balance_reader=fc.balance_reader, transfer_reader=fc.transfer_reader,
                               buyer_address=lambda _net: fc.address)


@pytest.fixture
def adeps(deps, fc):
    return wire(deps, fc)


def nonce_of(deps, pid: str) -> str:
    return [e.payload for e in deps.repo.list_evidence(pid) if e.kind == EvidenceKind.PAYMENT_PAYLOAD][-1]["nonce"]


async def buy(deps, intent: str = "i1", ticker: str = "ACME") -> dict[str, Any]:
    return await purchase(deps, "report", {"ticker": ticker}, intent)


async def buy_and_settle_on_chain(deps, fc: FakeChain, seller: FakeSeller, **kw) -> dict[str, Any]:
    r = await buy(deps, **kw)
    fc.spend(PAY_TO, 500_000, seller.tx, nonce_of(deps, r["purchase_id"]))
    return r


# ---- the eight cases from the issue ---------------------------------------------------------------------------

async def test_no_drift_is_ok_and_takes_one_baseline(adeps, fc):
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "ok" and w["unexplained_outflows"] == []
    (net,) = w["networks"]
    assert net["network"] == SEPOLIA and net["baseline"]["balance_minor"] == START == net["current"]["balance_minor"]
    assert w["headline"].startswith("✅") and "8.750000 USDC" in w["headline"]
    assert fc.scans == []  # balanced and nothing to prove: no log scan
    again = await audit.audit_wallet(adeps)
    assert again["status"] == "ok" and again["networks"][0]["baseline"] == net["baseline"]  # baseline is kept


async def test_outside_spend_is_an_unexplained_outflow(adeps, fc, seller):
    await buy_and_settle_on_chain(adeps, fc, seller)
    fc.spend(OUTSIDE, 10_000, "0x" + "0d" * 32)  # e.g. the bare SDK or another agent on the same key
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "drift"
    (o,) = w["unexplained_outflows"]
    assert o["code"] == FindingCode.UNEXPLAINED_OUTFLOW == "UNEXPLAINED_OUTFLOW" and o["severity"] == "proven"
    assert (o["tx"], o["to"], o["amount_minor"], o["amount_usdc"], o["block_number"]) == (
        "0x" + "0d" * 32, OUTSIDE, 10_000, "0.010000", fc.block)
    assert o["explorer_url"] == f"https://sepolia.basescan.org/tx/{o['tx']}" and o["safe_next_actions"] == ["human_review"]
    assert w["headline"].startswith("🔴 Paid outside PayGuard: 0.010000 USDC")
    # the PayGuard purchase itself is explained and gets on-chain proof in the same pass
    assert len(w["networks"][0]["confirmed_on_chain"]) == 1
    # recorded: the next audit reports it again without rescanning old blocks
    scans = list(fc.scans)
    again = await audit.audit_wallet(adeps)
    assert [x["tx"] for x in again["unexplained_outflows"]] == [o["tx"]] and fc.scans == scans


async def test_incoming_top_up_is_not_flagged(adeps, fc):
    await audit.audit_wallet(adeps)
    fc.receive(1_000_000)  # faucet
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "ok" and w["unexplained_outflows"] == []
    assert w["networks"][0]["incoming_minor"] == 1_000_000 and len(fc.scans) == 1  # drift -> one scan explains it


async def test_in_flight_payment_is_pending_not_flagged(make_deps, fc):
    seller = FakeSeller(mode="timeout")  # paid request stalls: the row ends SETTLEMENT_UNKNOWN
    deps = wire(make_deps(seller), fc)
    r = await buy(deps)
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN"
    w = await audit.audit_wallet(deps)  # transfer not on-chain yet
    assert w["status"] == "ok" and w["networks"][0]["pending_minor"] == 500_000
    fc.spend(PAY_TO, 500_000, "0x" + "77" * 32, nonce_of(deps, r["purchase_id"]))  # now it lands (seller never said)
    w = await audit.audit_wallet(deps)
    assert w["status"] == "ok" and w["unexplained_outflows"] == []
    assert deps.repo.get_purchase(r["purchase_id"]).payment_status == P.SETTLEMENT_UNKNOWN  # the audit never moves status


async def test_receipt_only_settlement_without_transfer_is_not_on_chain(adeps, fc, seller):
    r = await buy(adeps)  # the seller's receipt says SETTLED, but nothing ever left the wallet
    fc.ts_offset = audit.NOT_ON_CHAIN_GRACE_S + 5
    fc.block += 3
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "drift" and w["unexplained_outflows"] == []
    assert w["networks"][0]["settlements_not_on_chain"] == [r["purchase_id"]]
    assert w["headline"].startswith("🔴 1 recorded settlement")
    (f,) = [f for f in adeps.repo.findings_for_purchase(r["purchase_id"]) if f.code == FindingCode.SETTLEMENT_NOT_ON_CHAIN]
    assert f.severity == "proven" and f.safe_next_actions == ["human_review", "report_to_seller"] and f.evidence_ids
    row = adeps.repo.get_purchase(r["purchase_id"])
    assert row.payment_status == P.SETTLED  # recorded as a finding, not a status change
    resp = build_response(adeps, row)
    assert resp["risk"] == "red" and resp["safe_next_actions"][:2] == ["human_review", "report_to_seller"]
    assert compact_purchase(adeps, row)["risk"] == "red"
    await audit.audit_wallet(adeps)  # no duplicate finding
    assert sum(f.code == FindingCode.SETTLEMENT_NOT_ON_CHAIN for f in adeps.repo.findings_for_purchase(r["purchase_id"])) == 1


async def test_matched_row_upgrades_proof_to_on_chain(adeps, fc, seller):
    r = await buy_and_settle_on_chain(adeps, fc, seller)
    assert r["transactions"][0]["proof"] == "seller_receipt"
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "ok" and w["networks"][0]["confirmed_on_chain"] == [r["purchase_id"]]
    tx = build_response(adeps, adeps.repo.get_purchase(r["purchase_id"]))["transactions"][0]
    assert tx["proof"] == "chain_check" and tx["block_number"] == fc.block
    assert "proven on-chain" in compact_purchase(adeps, adeps.repo.get_purchase(r["purchase_id"]))["headline"]
    await audit.audit_wallet(adeps)  # idempotent: one chain SETTLEMENT evidence, and no more scans once proven
    ev = [e for e in adeps.repo.list_evidence(r["purchase_id"]) if e.kind == EvidenceKind.SETTLEMENT
          and e.payload.get("source") == audit.CHAIN_SOURCE]
    assert len(ev) == 1 and len(fc.scans) == 1


@pytest.mark.parametrize("where", ["balance", "scan", "address"])
async def test_rpc_errors_are_unavailable_never_raise(adeps, fc, seller, where):
    await buy_and_settle_on_chain(adeps, fc, seller)  # something to scan for
    if where == "balance":
        fc.fail_balance = True
    elif where == "scan":
        fc.fail_scan = True
    else:
        adeps = dataclasses.replace(adeps, buyer_address=lambda _n: (_ for _ in ()).throw(RuntimeError("BUYER_KEY_SEPOLIA is not set")))
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "unavailable" and w["networks"][0]["status"] == "unavailable"
    assert w["headline"].startswith("⚪")


async def test_one_network_down_does_not_hide_another(adeps, fc, monkeypatch):
    monkeypatch.setattr(audit, "_networks", lambda _d: ["eip155:8453", SEPOLIA])
    real = fc.balance_reader

    async def mainnet_down(network, address):
        if network == "eip155:8453":
            raise ConnectionError("mainnet rpc down")
        return await real(network, address)

    w = await audit.audit_wallet(dataclasses.replace(adeps, balance_reader=mainnet_down))
    assert w["status"] == "ok" and [n["status"] for n in w["networks"]] == ["unavailable", "ok"]
    assert "on Base Sepolia" in w["headline"] and "Base audit unavailable" in w["headline"]


def test_reset_clears_the_baseline(deps):
    deps.repo.save_wallet_state("test", SEPOLIA, {"address": "0xabc", "baseline_balance_minor": 1})
    deps.repo.save_wallet_state("other", SEPOLIA, {"address": "0xdef"})
    assert deps.repo.get_wallet_state("test", SEPOLIA)["baseline_balance_minor"] == 1
    deps.repo.reset("test")
    assert deps.repo.get_wallet_state("test", SEPOLIA) is None
    assert deps.repo.get_wallet_state("other", SEPOLIA) is not None  # another principal's baseline is untouched


# ---- wiring, safety and edges --------------------------------------------------------------------------------

async def test_paid_tool_takes_baseline_before_paying_and_never_scans(adeps, fc, seller):
    r = await buy(adeps)
    assert r["payment_status"] == "SETTLED"
    state = adeps.repo.get_wallet_state("test", SEPOLIA)
    assert state["baseline_balance_minor"] == START and state["baseline_ts"] <= adeps.repo.get_purchase(r["purchase_id"]).created_at
    await buy(adeps, intent="i2", ticker="BETA")
    assert fc.balance_calls == 1 and fc.scans == []  # one balanceOf for the baseline, nothing after it


async def test_paid_tool_survives_rpc_down(adeps, fc):
    fc.fail_balance = True
    r = await buy(adeps)
    assert r["payment_status"] == "SETTLED" and adeps.repo.get_wallet_state("test", SEPOLIA) is None


async def test_not_configured_is_unavailable(deps):
    w = await audit.audit_wallet(deps)  # tests' default Deps have no wallet readers
    assert w["status"] == "unavailable" and w["networks"] == []
    await audit.ensure_baseline(deps, SEPOLIA)
    assert deps.repo.get_wallet_state("test", SEPOLIA) is None


async def test_settled_row_older_than_baseline_is_not_judged(adeps, fc, seller, deps):
    r = await buy(deps)  # bought before the audit existed: no baseline, no transfer in scanned range
    fc.ts_offset = audit.NOT_ON_CHAIN_GRACE_S + 5
    w = await audit.audit_wallet(adeps)  # baseline now, after the purchase
    assert w["status"] == "ok" and not adeps.repo.has_finding(r["purchase_id"], FindingCode.SETTLEMENT_NOT_ON_CHAIN)


async def test_recent_settlement_gets_grace_before_judgement(adeps, fc, seller):
    r = await buy(adeps)  # SETTLED just now; the RPC does not show the transfer yet
    fc.block += 1
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "ok" and not adeps.repo.has_finding(r["purchase_id"], FindingCode.SETTLEMENT_NOT_ON_CHAIN)


async def test_wrong_amount_in_payguard_tx_is_not_explained(adeps, fc, seller):
    r = await buy(adeps)
    fc.spend(PAY_TO, 600_000, seller.tx, nonce_of(adeps, r["purchase_id"]))  # more than PayGuard authorized
    fc.ts_offset = audit.NOT_ON_CHAIN_GRACE_S + 5
    w = await audit.audit_wallet(adeps)
    assert [o["amount_minor"] for o in w["unexplained_outflows"]] == [600_000]
    assert w["networks"][0]["settlements_not_on_chain"] == [r["purchase_id"]]


async def test_match_by_nonce_when_seller_reports_another_tx(adeps, fc, seller):
    r = await buy(adeps)
    fc.spend(PAY_TO, 500_000, "0x" + "99" * 32, nonce_of(adeps, r["purchase_id"]))  # seller's receipt tx is wrong
    w = await audit.audit_wallet(adeps)
    assert w["status"] == "ok" and w["networks"][0]["confirmed_on_chain"] == [r["purchase_id"]]
    (ev,) = [e.payload for e in adeps.repo.list_evidence(r["purchase_id"]) if e.payload.get("source") == audit.CHAIN_SOURCE]
    assert ev["transaction"] == "0x" + "99" * 32 and ev["seller_reported_tx"] == seller.tx


async def test_money_is_integer_minor_units(adeps, fc, seller):
    await buy_and_settle_on_chain(adeps, fc, seller)
    fc.spend(OUTSIDE, 1, "0x" + "01" * 32)
    w = await audit.audit_wallet(adeps)
    net = w["networks"][0]

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                assert not isinstance(v, float) or k == "baseline_ts", k
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(w)
    assert net["current"]["balance_minor"] == START - 500_000 - 1 and w["unexplained_outflows"][0]["amount_usdc"] == "0.000001"


async def test_list_purchases_tool_has_wallet_block(adeps, fc, seller, monkeypatch):
    from payguard import server
    from payguard.server import mcp

    monkeypatch.setattr(server, "get_deps", lambda: adeps)
    await buy_and_settle_on_chain(adeps, fc, seller)
    fc.spend(OUTSIDE, 10_000, "0x" + "0d" * 32)
    tools = {t.name: t for t in await mcp.list_tools()}
    listing = await tools["list_purchases"].fn(limit=5)
    assert listing["wallet"]["status"] == "drift" and len(listing["wallet"]["unexplained_outflows"]) == 1
    assert listing["purchases"][0]["proof"] == "chain_check"  # the audit runs before the rows are built
    fc.fail_balance = True
    assert (await tools["list_purchases"].fn())["wallet"]["status"] == "unavailable"  # never fails the tool


def test_view_renders_wallet_line_and_outside_rows():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "payguard" / "ui" / "purchases.html").read_text()
    assert "renderWallet(data.wallet)" in html and "Paid outside PayGuard" in html and "unexplained_outflows" in html


# ---- the real readers in payguard/chain.py, over a fake web3 -------------------------------------------------

BUYER = "0x6CB1995c765366E8E5750aBE242402434ad8F7B9"


def _t(addr: str) -> HexBytes:
    return HexBytes("0x" + "00" * 12 + addr[2:].lower())


def _log(topic0: str, a: str, b: Any, block: int, tx: str, data: int = 0) -> dict[str, Any]:
    third = b if isinstance(b, HexBytes) else _t(b)
    return {"address": Web3.to_checksum_address(USDC[SEPOLIA]), "topics": [HexBytes(topic0), _t(a), third],
            "data": HexBytes(data.to_bytes(32, "big")), "blockNumber": block, "transactionHash": HexBytes(tx), "logIndex": 1}


class LogEth:
    def __init__(self, logs: list[dict[str, Any]], head: int) -> None:
        self.logs, self.head, self.queries = logs, head, []

    def get_block(self, _which):
        return {"number": self.head, "timestamp": 1_800_000_000}

    def get_logs(self, q):
        self.queries.append(q)
        t0, t1 = q["topics"][0], q["topics"][1]
        t0 = t0 if isinstance(t0, list) else [t0]
        t2 = q["topics"][2] if len(q["topics"]) > 2 else None
        return [lg for lg in self.logs if q["fromBlock"] <= lg["blockNumber"] <= q["toBlock"]
                and chain._hex(lg["topics"][0]) in t0
                and (t1 is None or chain._hex(lg["topics"][1]) == t1)
                and (t2 is None or chain._hex(lg["topics"][2]) == t2)]


def test_real_scan_reads_outgoing_incoming_and_nonces_in_200_block_windows(monkeypatch):
    tx_pay, tx_out, tx_in = "0x" + "aa" * 32, "0x" + "bb" * 32, "0x" + "cc" * 32
    nonce = "0x" + "5e" * 32
    logs = [_log(chain.TOPIC_USED, BUYER, HexBytes(nonce), 1_050, tx_pay),
            _log(chain.TOPIC_TRANSFER, BUYER, PAY_TO, 1_050, tx_pay, 500_000),
            _log(chain.TOPIC_TRANSFER, BUYER, OUTSIDE, 1_250, tx_out, 10_000),
            _log(chain.TOPIC_TRANSFER, FAUCET, BUYER, 1_300, tx_in, 1_000_000),
            _log(chain.TOPIC_TRANSFER, FAUCET, OUTSIDE, 1_300, "0x" + "dd" * 32, 7)]  # not ours
    eth = LogEth(logs, 1_400)
    monkeypatch.setattr(chain, "_w3", lambda _n: SimpleNamespace(eth=eth))
    s = chain._scan_transfers(SEPOLIA, BUYER, 1_001, 1_400, None)
    assert s.complete and s.scanned_to == 1_400
    assert [(t.tx_hash, t.to_addr, t.value_minor) for t in s.outgoing] == [(tx_pay, Web3.to_checksum_address(PAY_TO), 500_000),
                                                                          (tx_out, OUTSIDE, 10_000)]
    assert [(t.tx_hash, t.from_addr, t.value_minor) for t in s.incoming] == [(tx_in, Web3.to_checksum_address(FAUCET), 1_000_000)]
    assert s.nonces == {tx_pay: [nonce]}
    windows = sorted({(q["fromBlock"], q["toBlock"]) for q in eth.queries})
    assert windows == [(1_001, 1_200), (1_201, 1_400)] and all(b - a + 1 <= 200 for a, b in windows)


def test_real_scan_stops_at_deadline_and_reports_incomplete(monkeypatch):
    eth = LogEth([], 5_000)
    monkeypatch.setattr(chain, "_w3", lambda _n: SimpleNamespace(eth=eth))
    s = chain._scan_transfers(SEPOLIA, BUYER, 1_001, 5_000, time.monotonic() - 1)
    assert not s.complete and s.scanned_to == 1_000 and eth.queries == []


def test_real_balance_reads_at_one_block(monkeypatch):
    calls = []

    class Fn:
        def call(self, block_identifier):
            calls.append(block_identifier)
            return 8_750_000

    eth = LogEth([], 1_234)
    eth.contract = lambda address, abi: SimpleNamespace(functions=SimpleNamespace(balanceOf=lambda a: Fn()))
    monkeypatch.setattr(chain, "_w3", lambda _n: SimpleNamespace(eth=eth))
    b = chain._read_balance(SEPOLIA, BUYER)
    assert (b.balance_minor, b.block_number, b.block_timestamp) == (8_750_000, 1_234, 1_800_000_000) and calls == [1_234]


async def test_incomplete_scan_does_not_prove_absence(adeps, fc, seller):
    r = await buy(adeps)
    fc.ts_offset = audit.NOT_ON_CHAIN_GRACE_S + 5
    fc.block += 500

    async def partial(network, address, from_block, to_block, *, stop_at=None):
        return TransferScan([], [], {}, from_block + 99, False)

    w = await audit.audit_wallet(dataclasses.replace(adeps, transfer_reader=partial))
    assert not adeps.repo.has_finding(r["purchase_id"], FindingCode.SETTLEMENT_NOT_ON_CHAIN)
    assert w["networks"][0]["scanned_to_block"] < fc.block



# ---- review (#88): bounded store reads; findings from before a later baseline -----------------------------------

class CountingRepo:
    """Wraps a repository and counts per-row reads."""

    def __init__(self, inner):
        self.inner, self.calls = inner, {"list_evidence": 0, "findings_for_purchase": 0, "list_findings": 0}

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name in self.calls:
            def counted(*a, **kw):
                self.calls[name] += 1
                return attr(*a, **kw)
            return counted
        return attr


async def test_audit_store_reads_are_bounded(adeps, fc, seller):
    for i in range(3):
        await buy_and_settle_on_chain(adeps, fc, seller, intent=f"i{i}", ticker=f"T{i}")
    fc.ts_offset = 0
    seller.price_override = 9_000_000  # over the per-purchase cap: REJECTED rows cannot match an outflow
    await buy(adeps, intent="rej", ticker="REJ")
    await audit.audit_wallet(adeps)  # proves the three settled rows (one scan)
    counting = CountingRepo(adeps.repo)
    w = await audit.audit_wallet(dataclasses.replace(adeps, repo=counting))
    assert w["status"] == "ok"
    assert counting.calls["findings_for_purchase"] == 0 and counting.calls["list_findings"] == 1
    assert counting.calls["list_evidence"] == 3  # one per matchable row, none for the rejected row, none repeated


async def test_flag_from_before_a_later_baseline_is_still_reported(adeps, fc, seller):
    r = await buy(adeps)
    fc.ts_offset = audit.NOT_ON_CHAIN_GRACE_S + 5
    fc.block += 2
    await audit.audit_wallet(adeps)
    assert adeps.repo.has_finding(r["purchase_id"], FindingCode.SETTLEMENT_NOT_ON_CHAIN)
    state = adeps.repo.get_wallet_state("test", SEPOLIA)
    adeps.repo.save_wallet_state("test", SEPOLIA, {**state, "address": "0x" + "00" * 20})  # forces a fresh baseline
    w = await audit.audit_wallet(adeps)
    assert w["networks"][0]["settlements_not_on_chain"] == [r["purchase_id"]] and w["status"] == "drift"
