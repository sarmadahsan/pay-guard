import pytest

from payguard.models import RejectReason
from payguard.policy import MandateError, Policy, check_mandate, check_service_allowed, load_policy
from payguard.registry import SEPOLIA, USDC, load_registry


@pytest.fixture
def policy():
    return Policy(principal_id="p", total_budget_minor=5_000_000, per_purchase_cap_minor=2_500_000,
                  allowed_networks=(SEPOLIA,), allowed_services=("report", "report_premium", "convert"))


@pytest.fixture
def reg():
    return load_registry(base_url="http://seller.test")


def test_registry_timeouts_and_prices(reg):
    assert reg["research"].timeout_s == 15 and reg["report"].timeout_s == 40 and reg["convert"].timeout_s == 40
    assert reg["report"].accepted_price_minor == 500_000 and reg["report_premium"].accepted_price_minor == 500_000
    assert reg["research"].accepted_price_minor == 750_000 and reg["convert"].accepted_price_minor == 2_000_000
    assert reg["report"].asset == USDC[SEPOLIA]
    assert reg["report"].build_request({"ticker": "ACME"}) == ("GET", "http://seller.test/report/ACME", None)
    assert reg["research"].build_request({"topic": "a b"}) == ("GET", "http://seller.test/research-slow?topic=a%20b", None)
    assert reg["convert"].build_request({"doc_id": "d", "pages": 20}) == ("POST", "http://seller.test/convert", {"doc_id": "d", "pages": 20})


def test_mandate_accepts_exact_price(policy, reg):
    check_mandate(policy, reg["report"], 500_000, USDC[SEPOLIA], spent_minor=0)


def test_price_mismatch_above_accepted(policy, reg):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["report_premium"], 750_000, USDC[SEPOLIA], spent_minor=0)
    assert ei.value.code == RejectReason.PRICE_MISMATCH


def test_price_mismatch_wrong_asset(policy, reg):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["report"], 500_000, "0x0000000000000000000000000000000000000001", spent_minor=0)
    assert ei.value.code == RejectReason.PRICE_MISMATCH


def test_asset_comparison_is_case_insensitive(policy, reg):
    check_mandate(policy, reg["report"], 500_000, USDC[SEPOLIA].lower(), spent_minor=0)


def test_budget_exceeded_per_purchase_cap(reg):
    p = Policy("p", 5_000_000, 100_000, (SEPOLIA,), ("report",))
    with pytest.raises(MandateError) as ei:
        check_mandate(p, reg["report"], 500_000, USDC[SEPOLIA], spent_minor=0)
    assert ei.value.code == RejectReason.BUDGET_EXCEEDED


def test_budget_exceeded_remaining(policy, reg):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["convert"], 2_000_000, USDC[SEPOLIA], spent_minor=3_250_000)  # remaining 1.75
    assert ei.value.code == RejectReason.BUDGET_EXCEEDED


def test_service_allowed(policy, reg):
    check_service_allowed(policy, reg["report"])
    with pytest.raises(MandateError) as ei:
        check_service_allowed(policy, reg["research"])
    assert ei.value.code == RejectReason.POLICY_DENIED


def test_load_policy_yaml(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("principal_id: demo\ntotal_budget_minor: 5000000\nper_purchase_cap_minor: 2500000\n"
                 "allowed_networks: ['eip155:84532']\nallowed_services: [report]\n")
    p = load_policy(f)
    assert p == Policy("demo", 5_000_000, 2_500_000, ("eip155:84532",), ("report",))
    assert p.auto_dispute is False and p.to_dict()["auto_dispute"] is False  # default: the agent must ask


@pytest.mark.parametrize("text,expected", [("auto_dispute: true\n", True), ("auto_dispute: false\n", False), ("", False)])
def test_load_policy_auto_dispute_flag(tmp_path, text, expected):
    f = tmp_path / "policy.yaml"
    f.write_text(_BASE + text)
    assert load_policy(f).auto_dispute is expected


def test_shipped_policy_turns_auto_dispute_on():
    from pathlib import Path

    p = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")
    assert p.auto_dispute is True and p.to_dict()["auto_dispute"] is True


async def test_get_spending_policy_reports_auto_dispute(monkeypatch, deps):
    import json
    from dataclasses import replace

    import payguard.server as srv

    deps.policy = replace(deps.policy, auto_dispute=True)
    monkeypatch.setattr(srv, "get_deps", lambda: deps)
    out = await srv.mcp.call_tool("get_spending_policy", {})
    data = out.structured_content if hasattr(out, "structured_content") else json.loads(out[0].text)
    assert data["auto_dispute"] is True and data["principal_id"] == "p"


@pytest.mark.parametrize("bad", [-1_000_000, 0, True, 1.5, "500000"])
def test_non_positive_or_non_integer_amount_rejected(policy, reg, bad):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["report"], bad, USDC[SEPOLIA], spent_minor=0)
    assert ei.value.code == RejectReason.PRICE_MISMATCH


@pytest.mark.parametrize("bad", [-1, 1.0, True])
def test_invalid_spent_is_caller_bug(policy, reg, bad):
    with pytest.raises(ValueError):
        check_mandate(policy, reg["report"], 500_000, USDC[SEPOLIA], spent_minor=bad)


_BASE = "principal_id: d\ntotal_budget_minor: 5000000\nper_purchase_cap_minor: 2500000\n"


@pytest.mark.parametrize("text", [
    _BASE + "principal_id: dup\n",
    "",
    _BASE + "allowed_services: report\n",
    _BASE + "allowed_networks: [1, 2]\n",
    "principal_id: d\ntotal_budget_minor: 5000000\nper_purchase_cap_minor: 2.5\n",
    "principal_id: d\ntotal_budget_minor: -1\nper_purchase_cap_minor: 5\n",
    "principal_id: d\ntotal_budget_minor: true\nper_purchase_cap_minor: 5\n",
    "principal_id: d\ntotal_budget_minor: 5\nper_purchase_cap_minor: -5\n",
    _BASE + "auto_dispute: yes please\n",
    _BASE + "auto_dispute: 1\n",
    _BASE + "auto_dispute: [true]\n",
])
def test_load_policy_rejects_invalid(tmp_path, text):
    f = tmp_path / "policy.yaml"
    f.write_text(text)
    with pytest.raises(ValueError):
        load_policy(f)


def _bazaar_env(monkeypatch, price):
    monkeypatch.setenv("BAZAAR_URL", "http://bazaar.test/x")
    if price is None:
        monkeypatch.delenv("BAZAAR_PRICE_MINOR", raising=False)
    else:
        monkeypatch.setenv("BAZAAR_PRICE_MINOR", price)


@pytest.mark.parametrize("price", [None, "abc", "1.5", "0", "-3"])
def test_bazaar_invalid_price_skipped(monkeypatch, caplog, price):
    _bazaar_env(monkeypatch, price)
    reg = load_registry(base_url="http://seller.test")
    assert "bazaar" not in reg
    assert set(reg) == {"report", "report_premium", "research", "convert"}
    assert any("bazaar" in r.message.lower() for r in caplog.records)


def test_bazaar_valid_price(monkeypatch):
    _bazaar_env(monkeypatch, "10000")
    reg = load_registry(base_url="http://seller.test")
    assert reg["bazaar"].accepted_price_minor == 10_000


def test_wallet_client_registers_only_requested_network():
    from eth_account import Account

    from payguard.registry import MAINNET
    from payguard.wallet import client_for_account

    c = client_for_account(Account.create(), SEPOLIA)
    nets = {e["network"] for e in c.get_registered_schemes()[2]}
    assert nets == {SEPOLIA} and MAINNET not in nets
