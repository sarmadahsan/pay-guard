from payguard.models import (
    BUDGET_STATES,
    IN_FLIGHT,
    SAFE_NEXT_ACTION_PREFIXES,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    assert_transition,
    fingerprint,
)
import pytest


def test_fingerprint_is_canonical():
    a = fingerprint("report", {"ticker": "ACME", "x": 1})
    b = fingerprint("report", {"x": 1, "ticker": "ACME"})
    assert a == b and len(a) == 64
    assert fingerprint("report", {"ticker": "acme"}) != a


@pytest.mark.parametrize(
    "src,dst",
    [
        ("INTENT_RECORDED", "QUOTED"), ("QUOTED", "AUTHORIZED"), ("QUOTED", "REJECTED"),
        ("AUTHORIZED", "PAYLOAD_SIGNED"), ("AUTHORIZED", "REJECTED"), ("PAYLOAD_SIGNED", "SENT"),
        ("SENT", "SETTLED"), ("SENT", "SETTLEMENT_UNKNOWN"), ("SETTLEMENT_UNKNOWN", "SETTLED"),
        ("SETTLEMENT_UNKNOWN", "PAYMENT_FAILED"), ("INTENT_RECORDED", "ABANDONED"),
        ("QUOTED", "ABANDONED"), ("AUTHORIZED", "ABANDONED"), ("PAYLOAD_SIGNED", "SETTLEMENT_UNKNOWN"),
        ("INTENT_RECORDED", "REJECTED"), ("SETTLEMENT_UNKNOWN", "SETTLEMENT_UNKNOWN"),
    ],
)
def test_allowed_transitions(src, dst):
    assert_transition(PaymentStatus(src), PaymentStatus(dst))


@pytest.mark.parametrize(
    "src,dst",
    [("SETTLED", "SETTLEMENT_UNKNOWN"), ("REJECTED", "QUOTED"), ("SENT", "ABANDONED"),
     ("PAYLOAD_SIGNED", "ABANDONED"), ("PAYMENT_FAILED", "SETTLED"), ("SETTLED", "PAYMENT_FAILED")],
)
def test_forbidden_transitions(src, dst):
    with pytest.raises(ValueError):
        assert_transition(PaymentStatus(src), PaymentStatus(dst))


def test_budget_states_match_design():
    assert BUDGET_STATES == {PaymentStatus.AUTHORIZED, PaymentStatus.PAYLOAD_SIGNED, PaymentStatus.SENT,
                             PaymentStatus.SETTLEMENT_UNKNOWN, PaymentStatus.SETTLED}
    assert PaymentStatus.REJECTED not in BUDGET_STATES and PaymentStatus.ABANDONED not in BUDGET_STATES
    assert IN_FLIGHT == {PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, PaymentStatus.AUTHORIZED,
                         PaymentStatus.PAYLOAD_SIGNED, PaymentStatus.SENT}


def test_next_action_vocabulary_has_no_pay_or_retry():
    for v in SAFE_NEXT_ACTION_PREFIXES:
        assert "pay" not in v and "retry" not in v
    assert NextAction.wait_until(123).startswith("wait_until:")


def test_purchase_new_defaults():
    p = Purchase.new(principal_id="p", task_id=None, purchase_intent_id="i1", request_fingerprint="f" * 64,
                     service_id="report", args={"ticker": "ACME"}, network="eip155:84532")
    assert p.payment_status == PaymentStatus.INTENT_RECORDED
    assert p.fulfillment_status == FulfillmentStatus.PENDING
    assert p.attempt == 1 and p.history[0]["state"] == "INTENT_RECORDED"
    assert len(p.purchase_id) == 32


def test_purchase_new_snapshots_nested_args():
    args = {"ticker": "ACME", "opts": {"depth": 1}, "tags": ["a"]}
    p = Purchase.new(principal_id="p", task_id=None, purchase_intent_id="i1", request_fingerprint="f" * 64,
                     service_id="report", args=args, network="eip155:84532")
    args["opts"]["depth"] = 2
    args["tags"].append("b")
    assert p.args == {"ticker": "ACME", "opts": {"depth": 1}, "tags": ["a"]}
