"""Meridian Intel (hackathon demo): the double-charge seller bought through PayGuard.

The naive buyer (litigious-seller/buyer/server.py, tool fetch_intel) reads the seller's 402 "Payment failed ... retry"
as an unpaid failure and pays again. PayGuard must instead classify the post-settlement 402 as SETTLEMENT_UNKNOWN,
reconcile on-chain to SETTLED + NO_DELIVERY, and refuse the retry without a second payment.
"""
import importlib
import time

import pytest

from payguard.chain import ChainState
from payguard.purchase import purchase
from payguard.registry import SEPOLIA, USDC, load_registry
from tests.conftest import FakeSeller

MERIDIAN = "http://127.0.0.1:8403"


async def used_on_chain(_network, _from, _nonce, _pay_to, _value, _since):
    return ChainState(used=True, block_number=42, block_timestamp=int(time.time()), event="used",
                      tx_hash="0x" + "d0" * 32, transfer_ok=True)


# ---- registry ----

def test_intel_service_registered_only_with_env(monkeypatch):
    monkeypatch.setenv("INTEL_BASE_URL", "")
    assert "intel" not in load_registry(base_url="http://seller.test")

    monkeypatch.setenv("INTEL_BASE_URL", MERIDIAN + "/")  # trailing slash tolerated
    svc = load_registry(base_url="http://seller.test")["intel"]
    assert svc.tool_name == "get_intel_report" and svc.method == "GET"
    assert svc.network == SEPOLIA and svc.asset == USDC[SEPOLIA] and svc.accepted_price_minor == 500_000
    assert svc.expected_content_type == "application/json"
    assert svc.validators == ("content_type", "non_empty", "ticker_matches")
    assert svc.timeout_s == 40.0
    assert svc.build_request({"ticker": "AC/ME"}) == ("GET", f"{MERIDIAN}/intel/AC%2FME", None)
    assert svc.to_dict()["accepted_price_usdc"] == "0.50"


# ---- the double-charge trap, end to end against a fake Meridian ----

async def test_meridian_false_failure_is_caught_and_retry_refused(make_deps):
    seller = FakeSeller(mode="false_failure")
    deps = make_deps(seller, chain_reader=used_on_chain)

    first = await purchase(deps, "intel", {"ticker": "NVDA"}, "demo-intel-1")
    assert first["payment_status"] == "SETTLED" and first["fulfillment_status"] == "NO_DELIVERY"
    assert first["settlement_tx"] == "0x" + "d0" * 32  # proven on-chain, not by the seller's word
    assert [f["code"] for f in first["findings"]] == ["PAID_NO_DELIVERY"]
    assert first["safe_next_actions"] == ["human_review", "report_to_seller"]
    states = [h["state"] for h in deps.repo.get_purchase(first["purchase_id"]).history]
    assert states.index("SETTLEMENT_UNKNOWN") < states.index("SETTLED")  # the 402 was never trusted as "not charged"

    # The seller's lie is on record as evidence: a 402 with no PAYMENT-RESPONSE and a body asking for a retry.
    evidence = {e.evidence_id: e for e in deps.repo.list_evidence(first["purchase_id"])}
    seller_errors = [e.payload for e in evidence.values() if e.kind == "seller_error"]
    assert seller_errors and seller_errors[0]["status"] == 402
    assert seller_errors[0]["payment_response"] is None and seller_errors[0]["payment_required_error"] is None
    assert "retry" in seller_errors[0]["body"]
    for f in first["findings"]:
        assert f["evidence_ids"] and set(f["evidence_ids"]) <= set(evidence), f

    # The naive buyer's move: same purchase again. PayGuard returns the same row, flags it, and signs nothing new.
    again = await purchase(deps, "intel", {"ticker": "NVDA"}, "demo-intel-1")
    assert again["purchase_id"] == first["purchase_id"]
    assert "DUPLICATE_SUSPECTED" in [f["code"] for f in again["findings"]]
    assert again["payment_status"] == "SETTLED"

    fresh_intent = await purchase(deps, "intel", {"ticker": "NVDA"}, "demo-intel-2")  # new id, same request: still blocked
    assert fresh_intent["purchase_id"] == first["purchase_id"]
    assert "DUPLICATE_SUSPECTED" in [f["code"] for f in fresh_intent["findings"]]

    other = await purchase(deps, "intel", {"ticker": "AAPL"}, "demo-intel-3")  # the seller is now quarantined
    assert other["payment_status"] == "REJECTED" and other["reject_reason"] == "SERVICE_QUARANTINED"

    assert len(seller.paid) == 1 and len(seller.unpaid) == 1  # exactly one payment ever sent to Meridian
    for resp in (first, again, fresh_intent, other):
        assert not any("pay" in a.lower() or "retry" in a.lower() for a in resp["safe_next_actions"])
        for f in resp["findings"]:
            assert f["evidence_ids"], f


async def test_meridian_honest_delivery_verifies(make_deps):
    """Third attempt per payer on the real seller delivers; PayGuard verifies the body shape it actually returns."""
    seller = FakeSeller(mode="ok")
    deps = make_deps(seller)
    resp = await purchase(deps, "intel", {"ticker": "nvda"}, "demo-intel-ok")
    assert resp["payment_status"] == "SETTLED" and resp["fulfillment_status"] == "VERIFIED"
    assert resp["findings"] == []


# ---- server: the env-gated tool ----

@pytest.fixture
def reload_server(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # reloads must not re-read an operator .env
    import payguard.server as srv

    yield lambda: importlib.reload(srv)
    monkeypatch.setenv("INTEL_BASE_URL", "")
    importlib.reload(srv)


async def _tools(srv):
    return {t.name: t for t in await srv.mcp.list_tools()}


async def test_intel_tool_absent_without_env(monkeypatch, reload_server):
    monkeypatch.setenv("INTEL_BASE_URL", "")
    srv = reload_server()
    assert "get_intel_report" not in await _tools(srv)
    assert callable(srv.get_intel_report)  # the name stays taken, so BAZAAR_TOOL_NAME=get_intel_report is refused


async def test_intel_tool_registered_with_exact_params(monkeypatch, reload_server):
    monkeypatch.setenv("INTEL_BASE_URL", MERIDIAN)
    tool = (await _tools(reload_server()))["get_intel_report"]
    props = set(tool.parameters["properties"])
    assert props == {"ticker", "purchase_intent_id", "task_id"}
    assert not any("force" in p.lower() or "retry" in p.lower() for p in props)
    assert "$0.50 USDC on Base Sepolia" in tool.description and "Reuse the same purchase_intent_id" in tool.description
    assert (tool.meta or {}).get("ui") == {"resourceUri": "ui://payguard/purchases.html"}


async def test_intel_tool_routes_to_intel_service(monkeypatch, reload_server):
    monkeypatch.setenv("INTEL_BASE_URL", MERIDIAN)
    srv = reload_server()
    calls = []

    async def fake_purchase(deps, service_id, args, intent_id, task_id):
        calls.append((deps, service_id, args, intent_id, task_id))
        return {"ok": True}

    monkeypatch.setattr(srv, "purchase", fake_purchase)
    monkeypatch.setattr(srv, "get_deps", lambda: "DEPS")
    await srv.mcp.call_tool("get_intel_report", {"ticker": " nvda ", "purchase_intent_id": "pi-1", "task_id": "t-1"})
    assert calls == [("DEPS", "intel", {"ticker": "NVDA"}, "pi-1", "t-1")]  # normalized like get_company_report: same fingerprint


async def test_list_services_shows_intel_on_sepolia(monkeypatch, reload_server, deps):
    monkeypatch.setenv("INTEL_BASE_URL", MERIDIAN)
    srv = reload_server()
    monkeypatch.setattr(srv, "get_deps", lambda: deps)  # the deps fixture's registry carries `intel`, like a live .env would
    tools = await _tools(srv)
    listed = {s["id"]: s for s in await tools["list_services"].fn()}
    assert listed["intel"]["network"] == "eip155:84532" and listed["intel"]["tool_name"] == "get_intel_report"
