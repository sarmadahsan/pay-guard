import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastmcp import Client

from payguard.models import Finding
from payguard.presentation import headline, risk, short_tx, subject
from payguard.purchase import purchase
from payguard.response import build_response, compact_purchase
from payguard.server import PURCHASES_UI, mcp
from tests.conftest import FakeSeller

UI_FILE = Path(__file__).resolve().parents[1] / "payguard" / "ui" / "purchases.html"
UI_TOOLS = {"get_company_report", "get_research_report", "convert_document", "get_purchase", "list_purchases",
            "reconcile_purchase"}
TX = "0x17a2" + "0" * 53 + "3931e"


def delivery(level, passed=0, total=0, failed=(), basis=""):
    return {"level": level, "basis": basis, "checks_passed": passed, "checks_total": total, "failed": list(failed)}


def txs(status="settled", proof="seller_receipt", minor=500_000, tx=TX):
    usdc = f"{minor // 1_000_000}.{minor % 1_000_000:06d}"
    return [{"status": status, "proof": proof, "amount_minor": minor, "amount_usdc": usdc, "settlement_tx": tx}]


def finding(code, severity, resolved=None):
    return {"code": code, "severity": severity, "resolved_at": resolved}


# ---- risk: every payment status x delivery level the response layer can produce ----

@pytest.mark.parametrize("status, level, tx, findings, expected", [
    ("SETTLED", "verified", txs(), [], "green"),
    ("SETTLED", "verified", txs(proof="chain_check"), [finding("SETTLEMENT_UNKNOWN", "suspected", resolved=1.0)], "green"),
    ("SETTLED", "verified", txs(), [finding("DUPLICATE_SUSPECTED", "suspected")], "amber"),  # verified but still open
    ("SETTLED", "partial", txs(), [finding("FULFILLMENT_FAILED", "proven")], "red"),
    ("SETTLED", "partial", txs(), [], "red"),  # partial is red even before a finding is written
    ("SETTLED", "none", txs(proof="chain_check"), [finding("PAID_NO_DELIVERY", "proven")], "red"),
    ("SETTLED", "none", txs(), [], "red"),
    ("SETTLED", "unverified", txs(), [finding("FULFILLMENT_UNVERIFIED", "suspected")], "amber"),
    ("SETTLED", "unverified", txs(), [], "amber"),  # checks pending
    ("SETTLEMENT_UNKNOWN", "none", txs("unknown", "pending"), [finding("SETTLEMENT_UNKNOWN", "suspected")], "amber"),
    ("SETTLEMENT_UNKNOWN", "none", txs("unknown", "pending"), [finding("PAID_NO_DELIVERY", "proven")], "red"),
    ("IN_PROGRESS", "none", txs("in_progress", "pending"), [], "amber"),
    ("IN_PROGRESS", "none", [], [], "amber"),
    ("PAYMENT_FAILED", "none", [], [finding("PAYMENT_FAILED", "proven")], "red"),
    ("PAYMENT_FAILED", "none", [], [], "red"),
    ("REJECTED", "none", [], [], "amber"),  # price mismatch refused: nothing spent, task not done
    ("REJECTED", "none", [], [finding("BUDGET_EXCEEDED", "proven")], "amber"),  # proof of the refusal, not a loss
    ("REJECTED", "none", [], [finding("SERVICE_QUARANTINED", "proven")], "amber"),
    ("ABANDONED", "none", [], [], "amber"),
])
def test_risk_mapping(status, level, tx, findings, expected):
    assert risk(status, delivery(level), tx, findings) == expected


def test_risk_accepts_finding_objects():
    f = Finding(finding_id="f1", purchase_id="p1", code="PAID_NO_DELIVERY", severity="proven", summary="s",
                evidence_ids=[], safe_next_actions=[], created_at=0.0)
    assert risk("SETTLED", delivery("unverified"), txs(), [f]) == "red"


# ---- headline ----

def test_headline_verified_report():
    h = headline(payment_status="SETTLED", service_id="report", args={"ticker": "ACME"},
                 delivery=delivery("verified", 4, 4), transactions=txs(), findings=[])
    assert h == "🟢 Paid 0.500000 USDC · report ACME · verified 4/4 · tx 0x17a2…3931e · seller receipt"


def test_headline_partial_convert():
    h = headline(payment_status="SETTLED", service_id="convert", args={"doc_id": "d1", "pages": 20},
                 delivery=delivery("partial", 1, 2, ["pdf_page_count"]), transactions=txs(minor=2_000_000),
                 findings=[finding("FULFILLMENT_FAILED", "proven")])
    assert h == ("🔴 Paid 2.000000 USDC · convert d1 · partial 1/2 · failed pdf_page_count · tx 0x17a2…3931e"
                 " · seller receipt")


def test_headline_research_nothing_delivered_proven_on_chain():
    h = headline(payment_status="SETTLED", service_id="research", args={"topic": "agents"},
                 delivery=delivery("none"), transactions=txs(proof="chain_check", minor=750_000),
                 findings=[finding("PAID_NO_DELIVERY", "proven")])
    assert h.startswith("🔴 Paid 0.750000 USDC · research agents · nothing delivered")
    assert h.endswith("proven on-chain")


def test_headline_uses_integer_usdc_for_small_amounts():
    h = headline(payment_status="SETTLED", service_id="report", args={}, delivery=delivery("verified", 1, 1),
                 transactions=txs(minor=1000, tx=None), findings=[])
    assert h == "🟢 Paid 0.001000 USDC · report · verified 1/1 · seller receipt"
    assert not re.search(r"\d\.\d{1,5}(?!\d)", h)  # never a float-looking amount


@pytest.mark.parametrize("status, expected", [
    ("SETTLEMENT_UNKNOWN", "🟡 Sent 0.500000 USDC · report ACME · settlement unknown · reconcile before spending again"),
    ("REJECTED", "🟡 Refused · report ACME · price mismatch · nothing spent"),
    ("PAYMENT_FAILED", "🔴 Payment failed · report ACME · no money moved"),
    ("ABANDONED", "🟡 Abandoned · report ACME · nothing spent"),
    ("IN_PROGRESS", "🟡 Paying 0.500000 USDC · report ACME · not settled yet"),
])
def test_headline_other_statuses(status, expected):
    tx = txs("unknown", "pending") if status in ("SETTLEMENT_UNKNOWN", "IN_PROGRESS") else []
    h = headline(payment_status=status, service_id="report", args={"ticker": "ACME"}, delivery=delivery("none"),
                 transactions=tx, findings=[], reject_reason="PRICE_MISMATCH")
    assert h == expected


def test_subject_and_short_tx():
    assert subject({"topic": "  a   very\nlong " + "x" * 60}).endswith("…") and len(subject({"topic": "y" * 60})) == 40
    assert subject({"pages": 3}) is None and subject(None) is None
    assert short_tx(TX) == "0x17a2…3931e" and short_tx(None) is None and short_tx("0xabc") == "0xabc"


# ---- response.py: additive keys on real purchases ----

async def test_build_response_and_compact_carry_risk_and_headline(deps, seller):
    ok = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    assert ok["risk"] == "green" and ok["headline"].startswith("🟢 Paid 0.500000 USDC · report ACME · verified 5/5")
    bad = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i2")  # fake seller returns 12 pages
    assert bad["risk"] == "red" and "partial" in bad["headline"]
    refused = await purchase(deps, "report_premium", {"ticker": "ACME"}, "i3")
    assert refused["payment_status"] == "REJECTED" and refused["risk"] == "amber" and "nothing spent" in refused["headline"]
    rows = {r.purchase_id: compact_purchase(deps, r) for r in deps.repo.list_purchases("test")}
    for full in (ok, bad, refused):
        c = rows[full["purchase_id"]]
        assert (c["risk"], c["headline"]) == (full["risk"], full["headline"])
    assert rows[bad["purchase_id"]]["failed_checks"] == bad["delivery"]["failed"] != []


async def test_unknown_settlement_is_amber(make_deps):
    deps = make_deps(FakeSeller(mode="timeout"))
    r = await purchase(deps, "research", {"topic": "t"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["risk"] == "amber"
    assert build_response(deps, deps.repo.get_purchase(r["purchase_id"]))["headline"] == r["headline"]


# ---- server: MCP Apps wiring ----

async def test_ui_tools_carry_resource_uri():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert UI_TOOLS <= set(tools)
    for name, t in tools.items():
        ui = (t.meta or {}).get("ui")
        if name in UI_TOOLS:
            assert ui == {"resourceUri": PURCHASES_UI}, name
        else:
            assert ui is None, name


async def test_ui_resource_served_as_mcp_app():
    async with Client(mcp) as c:
        (content,) = await c.read_resource(PURCHASES_UI)
        listed = {str(r.uri): r for r in await c.list_resources()}
    assert content.mime_type == "text/html;profile=mcp-app"
    assert listed[PURCHASES_UI].mime_type == "text/html;profile=mcp-app"
    assert content.text == UI_FILE.read_text(encoding="utf-8")


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_ui_html_is_self_contained_and_read_only():
    html = UI_FILE.read_text(encoding="utf-8")
    p = _Tags()
    p.feed(html)
    for tag, attrs in p.tags:
        for key in ("src", "href", "action"):
            assert not re.match(r"\s*(https?:)?//", attrs.get(key) or ""), (tag, key, attrs)
        assert tag not in ("form", "button", "input", "iframe"), tag
    assert not re.search(r"""(src|href)\s*=\s*["']\s*(https?:)?//""", html, re.I)
    assert not re.search(r"<\s*(form|button)\b", html, re.I)
    assert not re.search(r"""createElement\(\s*["'](button|form|input)""", html)
    assert not re.search(r"\b(pay|retry)\b(?!\w)", re.sub(r"PayGuard|payguard", "", html), re.I)
    assert "tools/call" not in html  # the view never calls a tool, so it can never spend
    assert "ui/initialize" in html and "ui/notifications/tool-result" in html


def test_instructions_ask_for_headline_first():
    assert "headline" in mcp.instructions and "amber or red" in mcp.instructions
