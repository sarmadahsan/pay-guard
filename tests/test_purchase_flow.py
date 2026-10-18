from dataclasses import replace

import pytest

from payguard.models import PaymentStatus, fingerprint
from payguard.purchase import purchase
from tests.conftest import FakeSeller


def kinds(deps, pid):
    return [e.kind for e in deps.repo.list_evidence(pid)]


def codes(resp):
    return [f["code"] for f in resp["findings"]]


async def test_happy_path_report_settled_verified(deps, seller):
    r = await purchase(deps, "report", {"ticker": "ACME"}, "intent-1", "task-1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "VERIFIED"
    assert r["settlement_tx"] == seller.tx and r["settled_amount"] == 500_000 and r["currency"] == "USDC"
    assert r["artifact"]["json"]["ticker"] == "ACME" and r["safe_next_actions"] == ["use_artifact"]
    assert [h["state"] for h in r["history"]] == [
        "INTENT_RECORDED", "QUOTED", "AUTHORIZED", "PAYLOAD_SIGNED", "SENT", "SETTLED", "VERIFIED"]
    assert kinds(deps, r["purchase_id"]) == [
        "quote", "selected_requirement", "authorization", "payment_payload", "artifact", "settlement",
        "validator", "validator", "validator", "validator"]
    assert len(seller.paid) == 1 and deps.repo.spent_minor("test") == 500_000 and r["findings"] == []
    assert r["explorer_url"].startswith("https://sepolia.basescan.org/tx/")


async def test_signed_header_is_stored_and_row_is_sent_before_the_paid_request(deps, seller):
    seen = {}
    original = seller.handler

    def spy(request):
        if request.headers.get("PAYMENT-SIGNATURE"):
            row = deps.repo.find_by_key("test", "i", fingerprint("report", {"ticker": "ACME"}))[0]
            seen["status"] = row.payment_status
            seen["headers"] = [e.payload["header"] for e in deps.repo.list_evidence(row.purchase_id) if e.kind == "payment_payload"]
            seen["sent_header"] = request.headers["PAYMENT-SIGNATURE"]
        return original(request)

    seller.handler = spy
    await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert seen["status"] == PaymentStatus.SENT
    assert seen["headers"] == [seen["sent_header"]]  # identical header persisted before send


async def test_not_x402_route_rejected(make_deps):
    seller = FakeSeller(mode="plain200")
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "NOT_X402" and codes(r) == ["NOT_X402"]
    assert "payment_payload" not in kinds(deps, r["purchase_id"]) and deps.repo.spent_minor("test") == 0


async def test_v1_seller_rejected(make_deps):
    seller = FakeSeller(mode="v1")
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "UNSUPPORTED_X402_VERSION"


async def test_price_mismatch_premium_before_signing(deps, seller):
    r = await purchase(deps, "report_premium", {"ticker": "ACME"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "PRICE_MISMATCH" and codes(r) == ["PRICE_MISMATCH"]
    k = kinds(deps, r["purchase_id"])
    assert "quote" in k and "payment_payload" not in k and seller.paid == [] and deps.repo.spent_minor("test") == 0
    assert r["findings"][0]["severity"] == "proven" and r["findings"][0]["evidence_ids"]


async def test_wrong_asset_is_price_mismatch(make_deps):
    seller = FakeSeller(asset="0x" + "22" * 20)
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert r["reject_reason"] == "PRICE_MISMATCH" and seller.paid == []


async def test_budget_exceeded_on_remaining_budget(make_deps, seller):
    deps = make_deps(seller)
    assert (await purchase(deps, "convert", {"doc_id": "d", "pages": 20}, "i1"))["payment_status"] == "SETTLED"
    assert (await purchase(deps, "research", {"topic": "t"}, "i2"))["payment_status"] == "SETTLED"
    assert (await purchase(deps, "report", {"ticker": "A"}, "i3"))["payment_status"] == "SETTLED"
    r = await purchase(deps, "convert", {"doc_id": "d2", "pages": 20}, "i4")  # 3.25 spent, 1.75 left
    assert r["reject_reason"] == "BUDGET_EXCEEDED" and codes(r) == ["BUDGET_EXCEEDED"] and len(seller.paid) == 3


async def test_budget_exceeded_per_purchase_cap(make_deps, seller, policy):
    deps = make_deps(seller, policy_override=replace(policy, per_purchase_cap_minor=100_000))
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["reject_reason"] == "BUDGET_EXCEEDED" and seller.paid == []


async def test_policy_denied_service(make_deps, seller, policy):
    deps = make_deps(seller, policy_override=replace(policy, allowed_services=("convert",)))
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "POLICY_DENIED" and seller.unpaid == []


async def test_signing_failure_rejects_without_money(deps, seller):
    class Boom:
        async def create_payment_payload(self, *_a, **_k):
            raise RuntimeError("hsm down")

    deps.client_factory = lambda _n: Boom()
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["reject_reason"] == "SIGNING_FAILED" and seller.paid == []
    assert "payment_payload" not in kinds(deps, r["purchase_id"]) and deps.repo.spent_minor("test") == 0


@pytest.mark.parametrize("mode,expect_artifact", [
    ("timeout", False), ("settle_failed", False), ("http_502", False), ("no_header", True), ("bad_header", True)])
async def test_every_ambiguous_outcome_is_settlement_unknown(make_deps, mode, expect_artifact):
    seller = FakeSeller(mode=mode)
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["valid_before"] is not None
    assert codes(r) == ["SETTLEMENT_UNKNOWN"] and r["findings"][0]["severity"] == "suspected"
    assert "reconcile_purchase" in r["safe_next_actions"] and any(a.startswith("wait_until:") for a in r["safe_next_actions"])
    assert not any("pay" in a or "retry" in a for a in r["safe_next_actions"])
    k = kinds(deps, r["purchase_id"])
    assert "payment_payload" in k and "seller_error" in k and (("artifact" in k) == expect_artifact)
    hs = [h["state"] for h in r["history"]]
    assert hs[hs.index("SENT") + 1] == "SETTLEMENT_UNKNOWN"
    # One authorization only. M5's reconcile may resend the IDENTICAL header once; it never signs a second one.
    assert len({q.headers["PAYMENT-SIGNATURE"] for q in seller.paid}) == 1 and len(seller.paid) <= 2
    assert deps.repo.spent_minor("test") == 500_000  # unknown still counts as spent


async def test_compare_and_set_loss_aborts_before_signing(deps, seller):
    def steal(_request):
        row = deps.repo.find_by_key("test", "i", fingerprint("report", {"ticker": "A"}))[0]
        assert deps.repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.ABANDONED)

    seller.on_unpaid = steal
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "ABANDONED" and seller.paid == [] and "payment_payload" not in kinds(deps, r["purchase_id"])


async def test_same_intent_returns_existing_row_without_paying(deps, seller):
    a = await purchase(deps, "report", {"ticker": "A"}, "i")
    b = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert a["purchase_id"] == b["purchase_id"] and len(seller.paid) == 1


async def test_convert_without_pdf_validator_is_unverified_not_failed(deps, seller):
    # M2 has no pdf_page_count validator yet: KeyError -> FULFILLMENT_UNVERIFIED (design §5). M4 changes this to FAILED.
    r = await purchase(deps, "convert", {"doc_id": "d", "pages": 20}, "i")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] in ("UNVERIFIED", "FAILED")
    assert r["artifact"]["path"].endswith(".pdf") and "json" not in r["artifact"]


async def test_unknown_service(deps):
    r = await purchase(deps, "nope", {}, "i")
    assert "error" in r and r["safe_next_actions"] == ["human_review"]


async def test_hostile_deep_json_body_still_settles_unverified(make_deps, monkeypatch):
    seller = FakeSeller()
    monkeypatch.setattr(seller, "body_for", lambda _r: (b"[" * 900 + b"]" * 900, "application/json"))
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "SETTLED" and r["settlement_tx"] == seller.tx
    assert r["fulfillment_status"] == "UNVERIFIED" and len(seller.paid) == 1
    assert "FULFILLMENT_UNVERIFIED" in codes(r) and "PAID_NO_DELIVERY" not in codes(r)


async def test_unwritable_artifact_dir_still_settles_unverified(deps, seller, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    deps.artifact_dir = f
    r = await purchase(deps, "convert", {"doc_id": "d", "pages": 20}, "i")  # PDF body is written to disk
    assert r["payment_status"] == "SETTLED" and r["settlement_tx"] == seller.tx
    assert r["fulfillment_status"] == "UNVERIFIED" and len(seller.paid) == 1


async def test_classification_error_never_leaves_row_sent(deps, seller, monkeypatch):
    import payguard.purchase as pp

    def boom(*_a, **_k):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(pp, "classify_outcome", boom)
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and "seller_error" in kinds(deps, r["purchase_id"])


@pytest.mark.parametrize("case", ["v2_body_no_header", "json_array", "json_null", "bad_header"])
async def test_malformed_quote_rejected_not_stuck(make_deps, case):
    import httpx
    seller = FakeSeller()

    def handler(request):
        if case == "v2_body_no_header":
            return httpx.Response(402, json={"x402Version": 2})
        if case == "json_array":
            return httpx.Response(200, json=[1, 2])
        if case == "json_null":
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": "!!!notbase64"}, json={"x402Version": 2})

    seller.handler = handler
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "NOT_X402" and codes(r) == ["NOT_X402"]
    assert seller.paid == [] and "payment_payload" not in kinds(deps, r["purchase_id"])


@pytest.mark.parametrize("amount", ["0.5", "1e6", "-5", "", " 5", "1" * 31, "²", "５０００００", "٥٠٠٠٠٠"])
async def test_non_digit_amount_is_price_mismatch(make_deps, amount):
    seller = FakeSeller()
    orig = seller._requirements
    seller._requirements = lambda req: orig(req).model_copy(update={"amount": amount})
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "PRICE_MISMATCH" and seller.paid == []
    assert r["findings"][0]["evidence_ids"]


def _stalling_deps(make_deps, monkeypatch, seller, stall_on):
    import asyncio
    import httpx
    import payguard.purchase as pp

    monkeypatch.setattr(pp, "clip_timeout", lambda _t, _d: 0.05)
    deps = make_deps(seller)
    stalled = []

    async def handler(request):
        paid = bool(request.headers.get("PAYMENT-SIGNATURE"))
        if paid == (stall_on == "paid"):
            stalled.append(request)
            await asyncio.sleep(10)
        return seller.handler(request)

    deps.http_factory = lambda svc: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return deps, stalled


async def test_stalled_quote_hits_absolute_deadline(make_deps, seller, monkeypatch):
    deps, stalled = _stalling_deps(make_deps, monkeypatch, seller, "quote")
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "QUOTE_FAILED"
    assert seller.paid == [] and "payment_payload" not in kinds(deps, r["purchase_id"])


async def test_stalled_paid_request_hits_absolute_deadline(make_deps, seller, monkeypatch):
    deps, stalled = _stalling_deps(make_deps, monkeypatch, seller, "paid")
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and "seller_error" in kinds(deps, r["purchase_id"])
    assert len(stalled) == 1 and deps.repo.spent_minor("test") == 500_000
