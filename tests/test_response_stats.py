from payguard.models import EvidenceKind, FulfillmentStatus, PaymentStatus, Purchase
from payguard.purchase import purchase
from payguard.response import budget_summary, build_response, compact_purchase, usdc
from tests.conftest import FakeSeller

P = PaymentStatus
FP = "fp-" + "0" * 16


def drive(repo, row, *path):
    current = row.payment_status
    for nxt in path:
        assert repo.update_status(row.purchase_id, current, nxt)
        current = nxt
    return repo.get_purchase(row.purchase_id)


def seed(repo, status, fulfillment=FulfillmentStatus.PENDING, intent="i1", fp=FP, service="report", attempt=1):
    row = repo.create_purchase(Purchase.new(principal_id="test", task_id=None, purchase_intent_id=intent,
                                            request_fingerprint=fp, service_id=service, args={"ticker": "ACME"},
                                            network="eip155:84532", attempt=attempt))
    path = {P.SETTLED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLED)}[status]
    row = drive(repo, row, *path)
    if fulfillment != FulfillmentStatus.PENDING:
        repo.set_fulfillment(row.purchase_id, fulfillment)
    return repo.get_purchase(row.purchase_id)


async def test_verified_purchase_reports_transaction_delivery_and_budget(deps, seller):
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    assert r["delivery"] == {"level": "verified", "basis": "all checks passed", "checks_passed": 4, "checks_total": 4,
                             "failed": [], "artifact_sha256": r["artifact"]["sha256"], "proven_findings": []}
    (tx,) = r["transactions"]
    assert tx["status"] == "settled" and tx["proof"] == "seller_receipt" and tx["settlement_tx"] == seller.tx
    assert tx["amount_minor"] == 500_000 and tx["amount_usdc"] == "0.500000" and tx["network"] == "eip155:84532"
    assert tx["explorer_url"].startswith("https://sepolia.basescan.org/tx/")
    assert r["budget"] == {"total_budget_minor": 5_000_000, "spent_minor": 500_000, "remaining_minor": 4_500_000,
                           "spent_usdc": "0.500000", "remaining_usdc": "4.500000"}


async def test_rejected_purchase_has_no_transactions(deps, seller):
    r = await purchase(deps, "report_premium", {"ticker": "ACME"}, "i2")
    assert r["transactions"] == [] and r["delivery"]["level"] == "none" and r["budget"]["spent_minor"] == 0


async def test_unknown_settlement_is_a_pending_transaction(make_deps):
    deps = make_deps(FakeSeller(mode="timeout"))
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i3")
    (tx,) = r["transactions"]
    assert tx["status"] == "unknown" and tx["proof"] == "pending" and tx["valid_before"] == r["valid_before"]
    assert r["delivery"]["level"] == "none" and r["budget"]["spent_minor"] == 500_000  # unknown counts as spent


def test_partial_none_and_unverified_levels(deps):
    failed = seed(deps.repo, P.SETTLED, FulfillmentStatus.FAILED, intent="a")
    deps.repo.add_evidence(failed.purchase_id, EvidenceKind.ARTIFACT, {"kind": "pdf", "content_type": "application/pdf",
                           "sha256": "ab" * 32, "size": 10, "json": None, "path": "/x.pdf", "page_count": 12})
    deps.repo.add_evidence(failed.purchase_id, EvidenceKind.VALIDATOR, {"name": "content_type", "passed": True})
    deps.repo.add_evidence(failed.purchase_id, EvidenceKind.VALIDATOR, {"name": "pdf_page_count", "passed": False, "expected": 20, "actual": 12})
    d = build_response(deps, deps.repo.get_purchase(failed.purchase_id))["delivery"]
    assert d["level"] == "partial" and d["failed"] == ["pdf_page_count"] and d["checks_passed"] == 1 and d["checks_total"] == 2

    nothing = seed(deps.repo, P.SETTLED, FulfillmentStatus.NO_DELIVERY, intent="b")
    deps.repo.add_evidence(nothing.purchase_id, EvidenceKind.SETTLEMENT, {"source": "chain:AuthorizationUsed+Transfer", "transaction": "0x" + "c1" * 32, "block_number": 42})
    r = build_response(deps, deps.repo.get_purchase(nothing.purchase_id))
    assert r["delivery"]["level"] == "none" and r["delivery"]["basis"] == "settled with no artifact"
    assert r["transactions"][0]["proof"] == "chain_check" and r["transactions"][0]["block_number"] == 42

    unv = seed(deps.repo, P.SETTLED, FulfillmentStatus.UNVERIFIED, intent="c")
    assert build_response(deps, deps.repo.get_purchase(unv.purchase_id))["delivery"]["level"] == "unverified"


async def test_list_purchases_newest_first_with_levels(deps, seller):
    a = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    b = await purchase(deps, "report_premium", {"ticker": "ACME"}, "i2")
    rows = deps.repo.list_purchases("test", limit=10)
    assert [r.purchase_id for r in rows] == [b["purchase_id"], a["purchase_id"]]
    assert len(deps.repo.list_purchases("test", limit=1)) == 1
    assert deps.repo.list_purchases("someone-else") == []
    c = compact_purchase(deps, rows[1])
    assert set(c) >= {"purchase_id", "service_id", "payment_status", "fulfillment_status", "delivery_level",
                      "amount_usdc", "settlement_tx", "explorer_url", "created_at"}
    assert c["delivery_level"] == "verified" and c["amount_usdc"] == "0.500000"


def test_usdc_is_integer_formatted_and_handles_negatives():
    assert usdc(None) is None
    assert usdc(0) == "0.000000" and usdc(1) == "0.000001" and usdc(4_500_000) == "4.500000"
    assert usdc(-250_000) == "-0.250000"


async def test_list_purchases_tool_and_policy_budget(deps, monkeypatch):
    from payguard import server
    from payguard.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert "list_purchases" in tools and set(tools["list_purchases"].parameters["properties"]) <= {"limit"}

    monkeypatch.setattr(server, "get_deps", lambda: deps)
    seed(deps.repo, P.SETTLED, FulfillmentStatus.UNVERIFIED, intent="p")
    policy = await tools["get_spending_policy"].fn()
    assert {"spent_minor", "remaining_minor", "spent_usdc", "remaining_usdc", "total_budget_minor", "currency"} <= set(policy)
    assert policy["currency"] == "USDC" and policy == {**policy, **budget_summary(deps)}
    listing = await tools["list_purchases"].fn(limit=5)
    assert listing["count"] == 1 and listing["budget"] == budget_summary(deps)
