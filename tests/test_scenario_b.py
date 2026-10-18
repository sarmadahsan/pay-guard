"""Demo scenario B end to end: seller stalls after settling, chain proves the authorization was used."""
import time

from payguard.chain import ChainState
from payguard.purchase import purchase
from tests.conftest import FakeSeller


async def used_on_chain(_network, _from, _nonce, _pay_to, _value, _since):
    return ChainState(used=True, block_number=42, block_timestamp=int(time.time()), event="used",
                      tx_hash="0x" + "c1" * 32, transfer_ok=True)


async def test_scenario_b_end_to_end(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=used_on_chain)

    first = await purchase(deps, "research", {"topic": "agent payments"}, "demo-research-1")
    assert first["payment_status"] == "SETTLED" and first["fulfillment_status"] == "NO_DELIVERY"
    assert first["settlement_tx"] == "0x" + "c1" * 32  # proven by AuthorizationUsed + Transfer, so the demo shows a tx link
    assert [f["code"] for f in first["findings"]] == ["PAID_NO_DELIVERY"]
    assert first["safe_next_actions"] == ["human_review", "report_to_seller"]

    same = await purchase(deps, "research", {"topic": "agent payments"}, "demo-research-1")
    assert same["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in same["findings"]]

    new_intent = await purchase(deps, "research", {"topic": "agent payments"}, "demo-research-2")
    assert new_intent["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in new_intent["findings"]]

    reworded = await purchase(deps, "research", {"topic": "payments by agents"}, "demo-research-3")
    assert reworded["payment_status"] == "REJECTED" and reworded["reject_reason"] == "SERVICE_QUARANTINED"
    assert [f["code"] for f in reworded["findings"]] == ["SERVICE_QUARANTINED"]
    assert reworded["safe_next_actions"] == ["human_review"]

    assert len(seller.paid) == 1  # nonce consumed on chain, so reconcile never resends (reconcile.py step 3 resends only if unused)
    assert len({q.headers["PAYMENT-SIGNATURE"] for q in seller.paid}) == 1  # exactly one authorization ever signed
    for resp in (first, same, new_intent, reworded):
        for f in resp["findings"]:
            assert f["evidence_ids"], f  # every finding references evidence
        assert not any("pay" in a or "retry" in a for a in resp["safe_next_actions"])

    listed = deps.repo.list_findings("test", unresolved_only=True)
    assert {f["code"] for f in listed} == {"PAID_NO_DELIVERY", "DUPLICATE_SUSPECTED", "SERVICE_QUARANTINED"}
