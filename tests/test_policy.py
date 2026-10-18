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
    assert p.principal_id == "demo" and p.total_budget_minor == 5_000_000 and p.allowed_services == ("report",)
