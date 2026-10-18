"""Agent-filed disputes (Task 8.5): bundle, email, contact resolution, the two admin tools, both mail backends.

Offline: the seller is the FakeSeller MockTransport and Primitive is a second MockTransport. Nothing is sent.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from payguard.chain import ChainState
from payguard.disputes import build_bundle, check_dispute, file_dispute, render_email, resolve_contact
from payguard.models import EvidenceKind
from payguard.primitive import ATTACHMENT_NAME, DryRunBackend, PrimitiveBackend, build_backend
from payguard.purchase import purchase
from payguard.response import build_response, compact_purchase
from tests.conftest import FakeSeller

SIGNED_MARKERS = ("PAYMENT-SIGNATURE", "signature")


async def used_on_chain(_network, _from, _nonce, _pay_to, _value, _since):
    return ChainState(used=True, block_number=42, block_timestamp=int(time.time()), event="used",
                      tx_hash="0x" + "d0" * 32, transfer_ok=True)


class FakePrimitive:
    """Just enough of api.primitive.dev/v1: POST /send-mail and GET /sent-emails/{id}/reply."""

    def __init__(self) -> None:
        self.sends: list[httpx.Request] = []
        self.reply_calls = 0
        self.reply: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer prim_test_key"
        if request.method == "POST" and request.url.path.endswith("/send-mail"):
            self.sends.append(request)
            body = json.loads(request.content)
            return httpx.Response(200, json={"success": True, "data": {"id": "sent-123", "status": "queued", "from": body["from"]}})
        if request.method == "GET" and request.url.path.endswith("/sent-emails/sent-123/reply"):
            self.reply_calls += 1
            return httpx.Response(200, json={"success": True, "data": {"sent_email_id": "sent-123", "reply": self.reply,
                                                                     "waited": False, "timed_out": False}})
        return httpx.Response(404, json={"success": False, "error": {"code": "not_found", "message": "nope"}})

    def backend(self) -> PrimitiveBackend:
        return PrimitiveBackend("prim_test_key", "payguard@agent.primitive.test", base_url="https://api.primitive.test/v1",
                                transport=httpx.MockTransport(self.handler))


@pytest.fixture
def primitive() -> FakePrimitive:
    return FakePrimitive()


@pytest.fixture
def contact_env(monkeypatch):
    monkeypatch.setenv("SELLER_CONTACT_EMAIL", "support@seller.test")
    monkeypatch.delenv("PRIMITIVE_API_KEY", raising=False)
    monkeypatch.delenv("PRIMITIVE_FROM", raising=False)


def _with_contact(deps, email: str = "support@seller.test"):
    from dataclasses import replace

    deps.registry = {k: replace(s, contact_email=email) for k, s in deps.registry.items()}
    return deps


# ---- the three tests from the plan ------------------------------------------------------------------------

async def test_bundle_has_findings_and_evidence_and_redacts_signature(deps, seller):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")  # FULFILLMENT_FAILED (12 pages)
    b = build_bundle(deps, r["purchase_id"])
    assert b["purchase"]["purchase_id"] == r["purchase_id"] and b["purchase"]["service_id"] == "convert"
    assert b["seller"]["pay_to"] and b["seller"]["network"] == "eip155:84532"
    assert "FULFILLMENT_FAILED" in {f["code"] for f in b["findings"]}
    assert {"validator", "artifact", "payment_payload", "settlement"} <= {e["kind"] for e in b["evidence"]}
    text = json.dumps(b)
    assert "PAYMENT-SIGNATURE" not in text
    for e in b["evidence"]:
        if e["kind"] == "payment_payload":
            assert e["payload"]["header"] == "<redacted>" and "signature" not in e["payload"]
            assert e["payload"]["nonce"] and e["payload"]["from"]  # the on-chain facts stay
    assert b["purchase"]["buyer_address"] == next(e["payload"]["from"] for e in b["evidence"] if e["kind"] == "payment_payload")
    assert b["explorer_url"].startswith("https://sepolia.basescan.org/")


async def test_render_email_is_plain_text_with_the_facts(deps, seller):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    b = build_bundle(deps, r["purchase_id"])
    subject, body = render_email(b)
    assert subject.startswith("Dispute: ") and r["purchase_id"] in subject
    assert "2.000000 USDC" in body and "pdf_page_count" in body and b["explorer_url"] in body
    assert "deliver what was purchased, or refund 2.000000 USDC to the buyer address" in body
    assert b["purchase"]["buyer_address"] in body and "reply" in body.lower()
    assert "<redacted>" not in body and "PAYMENT-SIGNATURE" not in body


async def test_bundle_refuses_rows_without_unresolved_findings(deps, seller):
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i-ok")
    with pytest.raises(ValueError):
        build_bundle(deps, r["purchase_id"])


# ---- bundle for the demo's PAID_NO_DELIVERY row (Meridian's false failure) --------------------------------

async def test_bundle_for_paid_no_delivery_carries_the_402_and_chain_proof(make_deps):
    deps = make_deps(FakeSeller(mode="false_failure"), chain_reader=used_on_chain)
    r = await purchase(deps, "intel", {"ticker": "NVDA"}, "demo-1")
    assert [f["code"] for f in r["findings"]] == ["PAID_NO_DELIVERY"]
    b = build_bundle(deps, r["purchase_id"])
    kinds = {e["kind"] for e in b["evidence"]}
    assert {"seller_error", "chain_check", "settlement"} <= kinds
    assert b["explorer_url"] == "https://sepolia.basescan.org/tx/0x" + "d0" * 32
    _, body = render_email(b)
    assert "HTTP 402 after settlement" in body and "retry" in body and b["explorer_url"] in body


# ---- contact resolution -------------------------------------------------------------------------------------

async def test_contact_prefers_well_known_then_registry_then_none(deps, seller):
    svc = deps.registry["intel"]
    seller.support_email = "disputes@meridian.test"
    assert await resolve_contact(deps, svc) == ("disputes@meridian.test", "well_known")
    seller.support_email = None  # 404 -> registry field (from SELLER_CONTACT_EMAIL)
    _with_contact(deps, "fallback@seller.test")
    assert await resolve_contact(deps, deps.registry["intel"]) == ("fallback@seller.test", "registry")
    assert await resolve_contact(deps, None) == (None, "none")


def test_registry_fills_contact_email_from_env(monkeypatch):
    from payguard.registry import load_registry

    monkeypatch.setenv("SELLER_CONTACT_EMAIL", " ops@seller.test ")
    reg = load_registry(base_url="http://seller.test")
    assert {s.contact_email for s in reg.values()} == {"ops@seller.test"}
    monkeypatch.setenv("SELLER_CONTACT_EMAIL", "")
    assert all(s.contact_email is None for s in load_registry(base_url="http://seller.test").values())


# ---- file_dispute ---------------------------------------------------------------------------------------------

async def test_file_dispute_refuses_a_verified_purchase(deps, seller, contact_env):
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i-ok")
    out = await file_dispute(_with_contact(deps), r["purchase_id"], backend=DryRunBackend(deps.artifact_dir))
    assert "error" in out and "no unresolved proven finding" in out["error"]
    assert not [e for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == EvidenceKind.DISPUTE]
    assert not (deps.artifact_dir / "disputes").exists()
    assert await file_dispute(deps, "nope") == {"error": "purchase nope not found"}


async def test_file_dispute_without_any_contact_is_refused(deps, seller):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    out = await file_dispute(deps, r["purchase_id"], backend=DryRunBackend(deps.artifact_dir))
    assert "no contact address" in out["error"] and out["safe_next_actions"] == ["human_review"]


async def test_file_dispute_dry_run_writes_file_records_evidence_and_is_once_only(deps, seller, contact_env):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = r["purchase_id"]
    out = await file_dispute(_with_contact(deps), pid, backend=DryRunBackend(deps.artifact_dir))
    assert out["purchase_id"] == pid and out["dispute_id"] == f"local-{pid}" and out["to"] == "support@seller.test"
    assert out["subject"].startswith("Dispute: convert") and len(out["body_preview"]) <= 400 and out["body_preview"]
    assert out["evidence_count"] >= 4 and out["explorer_url"].startswith("https://sepolia.basescan.org/tx/")
    assert out["backend"] == "dry_run" and out["safe_next_actions"] == ["human_review", "check_dispute"]
    assert out["filed_by"] == "agent"

    path = deps.artifact_dir / "disputes" / f"{pid}.txt"
    text = path.read_text()
    assert "Subject: " + out["subject"] in text and "2.000000 USDC" in text and ATTACHMENT_NAME in text
    assert "PAYMENT-SIGNATURE" not in text and '"signature"' not in text and "<redacted>" in text

    ev = [e for e in deps.repo.list_evidence(pid) if e.kind == EvidenceKind.DISPUTE]
    assert len(ev) == 1
    assert {"dispute_id", "to", "from", "subject", "sent_at", "backend"} <= set(ev[0].payload)
    assert ev[0].payload["backend"] == "dry_run" and ev[0].payload["contact_source"] == "registry"
    assert deps.repo.get_purchase(pid).history[-1]["state"] == "DISPUTE_FILED"

    again = await file_dispute(deps, pid, backend=DryRunBackend(deps.artifact_dir))  # second call: no second email
    assert again["dispute_id"] == out["dispute_id"] and again["already_filed"] is True and again["to"] == out["to"]
    assert len([e for e in deps.repo.list_evidence(pid) if e.kind == EvidenceKind.DISPUTE]) == 1
    assert [h["state"] for h in deps.repo.get_purchase(pid).history].count("DISPUTE_FILED") == 1


async def test_file_dispute_posts_to_primitive_with_redacted_attachment(deps, seller, primitive):
    seller.support_email = "agent@meridian.test"  # the well-known endpoint wins over the registry fallback
    _with_contact(deps, "fallback@seller.test")
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = r["purchase_id"]
    out = await file_dispute(deps, pid, backend=primitive.backend())
    assert out["dispute_id"] == "sent-123" and out["to"] == "agent@meridian.test" and out["backend"] == "primitive"

    (req,) = primitive.sends
    assert req.url == "https://api.primitive.test/v1/send-mail"
    body = json.loads(req.content)
    assert body["from"] == "payguard@agent.primitive.test" and body["to"] == "agent@meridian.test"
    assert body["subject"] == out["subject"] and body["body_text"].startswith(out["body_preview"]) and body["wait"] is False
    (att,) = body["attachments"]
    assert att["filename"] == ATTACHMENT_NAME and att["content_type"] == "application/json"
    import base64

    bundle = json.loads(base64.b64decode(att["content_base64"]))
    assert bundle["purchase"]["purchase_id"] == pid and bundle["seller"]["contact_email"] == "agent@meridian.test"
    raw = req.content.decode()
    assert "PAYMENT-SIGNATURE" not in raw and '"signature"' not in raw and "0x" + "ab" * 32 in raw  # tx stays, token goes
    assert "<redacted>" in json.dumps(bundle)

    ev = [e for e in deps.repo.list_evidence(pid) if e.kind == EvidenceKind.DISPUTE]
    assert ev[0].payload["dispute_id"] == "sent-123" and ev[0].payload["contact_source"] == "well_known"
    assert not (deps.artifact_dir / "disputes").exists()  # the real backend writes no local file


async def test_file_dispute_primitive_error_records_nothing(deps, seller, contact_env):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"success": False, "error": {"code": "unauthorized", "message": "bad key"}})

    backend = PrimitiveBackend("prim_bad", "a@b.test", base_url="https://api.primitive.test/v1", transport=httpx.MockTransport(boom))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await file_dispute(_with_contact(deps), r["purchase_id"], backend=backend)
    assert not [e for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == EvidenceKind.DISPUTE]


# ---- check_dispute --------------------------------------------------------------------------------------------

async def test_check_dispute_records_a_reply_once(deps, seller, primitive):
    _with_contact(deps)
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = r["purchase_id"]
    assert (await check_dispute(deps, pid, backend=primitive.backend()))["error"].startswith("no dispute filed")
    await file_dispute(deps, pid, backend=primitive.backend())

    waiting = await check_dispute(deps, pid, backend=primitive.backend())
    assert waiting == {"purchase_id": pid, "dispute_id": "sent-123", "to": "support@seller.test", "status": "awaiting_reply",
                       "new_replies": 0, "replies": [], "safe_next_actions": ["human_review"]}

    primitive.reply = {"id": "in-9", "thread_id": "th-1", "reply_to_sent_email_id": "sent-123", "from_email": "agent@meridian.test",
                       "to_email": "payguard@agent.primitive.test", "subject": "Re: Dispute", "body_text": "x" * 5000,
                       "body_html": None, "received_at": "2026-10-18T10:00:00Z", "status": "received", "sender_verified": True}
    first = await check_dispute(deps, pid, backend=primitive.backend())
    assert first["status"] == "replied" and first["new_replies"] == 1 and len(first["replies"]) == 1
    reply = first["replies"][0]
    assert reply["reply_id"] == "in-9" and reply["from"] == "agent@meridian.test" and reply["sender_verified"] is True
    assert len(reply["body_text"]) == 4000  # capped
    assert first["safe_next_actions"] == ["human_review"]

    second = await check_dispute(deps, pid, backend=primitive.backend())  # idempotent
    assert second["new_replies"] == 0 and len(second["replies"]) == 1 and second["status"] == "replied"
    evidence = deps.repo.list_evidence(pid)
    assert len([e for e in evidence if e.kind == EvidenceKind.DISPUTE_REPLY]) == 1
    history = [h["state"] for h in deps.repo.get_purchase(pid).history]
    assert history.count("DISPUTE_REPLY") == 1 and history.index("DISPUTE_FILED") < history.index("DISPUTE_REPLY")

    # The finding is still open: a reply is surfaced, never acted on.
    row = deps.repo.get_purchase(pid)
    assert [f.code for f in deps.repo.findings_for_purchase(pid) if f.resolved_at is None] == ["FULFILLMENT_FAILED"]
    resp = build_response(deps, row)
    assert resp["dispute"] == {"dispute_id": "sent-123", "to": "support@seller.test", "sent_at": evidence[-2].payload["sent_at"],
                               "backend": "primitive", "filed_by": "agent", "status": "replied", "reply_count": 1}
    assert resp["headline"].endswith("· seller replied") and resp["risk"] == "red"
    assert "human_review" in resp["safe_next_actions"] and not any("pay" in a for a in resp["safe_next_actions"])
    assert resp["safe_next_actions"][-1] == "check_dispute"


async def test_check_dispute_dry_run_reads_a_dropped_reply_file(deps, seller, contact_env):
    _with_contact(deps)
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = r["purchase_id"]
    await file_dispute(deps, pid, backend=DryRunBackend(deps.artifact_dir))
    assert (await check_dispute(deps, pid))["status"] == "awaiting_reply"  # env has no key -> dry run
    (deps.artifact_dir / "disputes" / f"{pid}.reply.txt").write_text("We will refund.")
    out = await check_dispute(deps, pid)
    assert out["status"] == "replied" and out["replies"][0]["body_text"] == "We will refund."
    assert (await check_dispute(deps, pid))["new_replies"] == 0


# ---- ledger views and the MCP tools ---------------------------------------------------------------------------

async def test_responses_show_the_dispute(deps, seller, contact_env):
    _with_contact(deps)
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    row = deps.repo.get_purchase(r["purchase_id"])
    assert r["dispute"] is None and compact_purchase(deps, row)["dispute"] is None
    assert "dispute" not in r["headline"]
    out = await file_dispute(deps, row.purchase_id, backend=DryRunBackend(deps.artifact_dir))
    full, compact = build_response(deps, row), compact_purchase(deps, row)
    expected = {"dispute_id": out["dispute_id"], "to": "support@seller.test", "sent_at": out["sent_at"], "backend": "dry_run",
                "filed_by": "agent", "status": "awaiting_reply", "reply_count": 0}
    assert full["dispute"] == expected and compact["dispute"] == expected
    assert full["headline"].endswith("· dispute filed") and compact["headline"].endswith("· dispute filed")
    assert full["safe_next_actions"] == ["human_review", "report_to_seller", "check_dispute"]
    assert full["findings"] and full["findings"][0]["resolved_at"] is None


async def test_mcp_tools_route_to_disputes(monkeypatch):
    import payguard.server as srv

    calls = []

    async def fake_file(deps, pid):
        calls.append(("file", deps, pid))
        return {"ok": 1}

    async def fake_check(deps, pid):
        calls.append(("check", deps, pid))
        return {"ok": 2}

    monkeypatch.setattr(srv.disputes, "file_dispute", fake_file)
    monkeypatch.setattr(srv.disputes, "check_dispute", fake_check)
    monkeypatch.setattr(srv, "get_deps", lambda: "DEPS")
    await srv.mcp.call_tool("file_dispute", {"purchase_id": " p1 "})
    await srv.mcp.call_tool("check_dispute", {"purchase_id": "p1"})
    assert calls == [("file", "DEPS", "p1"), ("check", "DEPS", "p1")]
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    for name in ("file_dispute", "check_dispute"):
        assert set(tools[name].parameters["properties"]) == {"purchase_id"}
        assert "pay" not in name and "force" not in tools[name].description.lower()


# ---- backend selection ------------------------------------------------------------------------------------------

def test_build_backend_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PRIMITIVE_API_KEY", raising=False)
    monkeypatch.delenv("PRIMITIVE_FROM", raising=False)
    assert isinstance(build_backend(tmp_path), DryRunBackend)
    monkeypatch.setenv("PRIMITIVE_API_KEY", "prim_x")
    with pytest.raises(ValueError, match="PRIMITIVE_FROM"):
        build_backend(tmp_path)
    monkeypatch.setenv("PRIMITIVE_FROM", "agent@example.primitive.dev")
    b = build_backend(tmp_path)
    assert isinstance(b, PrimitiveBackend) and b.base_url == "https://api.primitive.dev/v1"
    monkeypatch.setenv("PRIMITIVE_API_BASE_URL", "https://api.example.test/v1/")
    assert build_backend(tmp_path).base_url == "https://api.example.test/v1"


def test_dry_run_rejects_unsafe_purchase_ids(tmp_path):
    with pytest.raises(ValueError):
        DryRunBackend(tmp_path).path_for("../etc")


# ---- auto_dispute (Task 8.6): the mandate files the dispute inside the purchase call ------------------------------

def _auto(deps, on: bool = True):
    from dataclasses import replace

    deps.policy = replace(deps.policy, auto_dispute=on)
    return deps


def _disputes(deps, pid):
    return [e for e in deps.repo.list_evidence(pid) if e.kind == EvidenceKind.DISPUTE]


def _auto_failures(deps, pid):
    from payguard.disputes import AUTO_FAILED

    return [e for e in deps.repo.list_evidence(pid) if e.kind == EvidenceKind.SELLER_ERROR and e.payload.get("reason") == AUTO_FAILED]


async def test_auto_dispute_files_in_call_for_a_failed_delivery(deps, seller, contact_env):
    _auto(_with_contact(deps))
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")  # FULFILLMENT_FAILED (12 pages)
    pid = r["purchase_id"]
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "FAILED" and len(seller.paid) == 1
    assert [f["code"] for f in r["findings"]] == ["FULFILLMENT_FAILED"] and r["findings"][0]["resolved_at"] is None

    # the response of the purchase call itself already shows the dispute
    assert r["dispute"]["filed_by"] == "policy" and r["dispute"]["status"] == "awaiting_reply"
    assert r["dispute"]["dispute_id"] == f"local-{pid}" and r["dispute"]["backend"] == "dry_run" and r["dispute"]["to"] == "support@seller.test"
    assert r["headline"].endswith("· dispute filed") and r["risk"] == "red"
    assert r["safe_next_actions"] == ["human_review", "report_to_seller", "check_dispute"]
    filed = [h for h in r["history"] if h["state"] == "DISPUTE_FILED"]
    assert len(filed) == 1 and filed[0]["dispute_filed"] is True and filed[0]["filed_by"] == "policy"
    assert r["history"][-1]["state"] == "DISPUTE_FILED"  # after FULFILLMENT_FAILED, inside the same call

    (ev,) = _disputes(deps, pid)
    assert ev.payload["filed_by"] == "policy" and ev.payload["finding_codes"] == ["FULFILLMENT_FAILED"]
    text = (deps.artifact_dir / "disputes" / f"{pid}.txt").read_text()
    assert "Subject: Dispute: convert" in text and "2.000000 USDC" in text and "PAYMENT-SIGNATURE" not in text
    assert not _auto_failures(deps, pid)

    # the agent's own file_dispute afterwards is a no-op that returns the policy's dispute
    again = await file_dispute(deps, pid, backend=DryRunBackend(deps.artifact_dir))
    assert again["already_filed"] is True and again["filed_by"] == "policy" and len(_disputes(deps, pid)) == 1


async def test_auto_dispute_off_files_nothing(deps, seller, contact_env):
    _auto(_with_contact(deps), on=False)
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    assert [f["code"] for f in r["findings"]] == ["FULFILLMENT_FAILED"]
    assert r["dispute"] is None and not _disputes(deps, r["purchase_id"]) and not (deps.artifact_dir / "disputes").exists()
    assert "dispute" not in r["headline"] and "check_dispute" not in r["safe_next_actions"]
    assert not any(h["state"] == "DISPUTE_FILED" for h in r["history"])


async def test_auto_dispute_leaves_a_verified_purchase_alone(deps, seller, contact_env):
    _auto(_with_contact(deps))
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i-ok")
    assert r["fulfillment_status"] == "VERIFIED" and r["findings"] == [] and r["risk"] == "green"
    assert r["dispute"] is None and not _disputes(deps, r["purchase_id"]) and not (deps.artifact_dir / "disputes").exists()
    assert not _auto_failures(deps, r["purchase_id"]) and r["safe_next_actions"] == ["use_artifact"]


async def test_auto_dispute_files_for_a_blocked_overcharge(deps, seller, contact_env):
    _auto(_with_contact(deps))
    r = await purchase(deps, "report_premium", {"ticker": "ACME"}, "i-premium")  # quoted 0.75 > accepted 0.50
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "PRICE_MISMATCH" and seller.paid == []
    assert r["dispute"]["filed_by"] == "policy" and r["headline"].endswith("· dispute filed")
    assert r["safe_next_actions"] == ["get_purchase", "check_dispute"]  # a refusal: nothing paid, nothing to review
    (ev,) = _disputes(deps, r["purchase_id"])
    assert ev.payload["finding_codes"] == ["PRICE_MISMATCH"]


async def test_auto_dispute_send_failure_never_changes_the_outcome(deps, seller, contact_env, monkeypatch):
    import payguard.disputes as d

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"success": False, "error": {"code": "unauthorized", "message": "bad key"}})

    bad = PrimitiveBackend("prim_bad", "a@b.test", base_url="https://api.primitive.test/v1", transport=httpx.MockTransport(boom))
    monkeypatch.setattr(d, "build_backend", lambda *_a, **_k: bad)
    _auto(_with_contact(deps))
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = r["purchase_id"]
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "FAILED" and len(seller.paid) == 1
    assert [f["code"] for f in r["findings"]] == ["FULFILLMENT_FAILED"] and r["risk"] == "red"
    assert r["dispute"] is None and not _disputes(deps, pid) and "dispute" not in r["headline"]
    assert r["safe_next_actions"] == ["human_review", "report_to_seller"]  # the agent can still file it
    (err,) = _auto_failures(deps, pid)
    assert err.payload["stage"] == "auto_dispute" and "HTTP 401" in err.payload["error"]
    assert not any(h["state"] == "DISPUTE_FILED" for h in r["history"])
    assert r["payment_status"] == deps.repo.get_purchase(pid).payment_status == "SETTLED"

    # the explicit tool still works once the key is fixed: one dispute, one failure record
    out = await file_dispute(deps, pid, backend=DryRunBackend(deps.artifact_dir))
    assert out["filed_by"] == "agent" and len(_disputes(deps, pid)) == 1 and len(_auto_failures(deps, pid)) == 1


async def test_auto_dispute_without_a_contact_records_the_failure_only(deps, seller):
    _auto(deps)  # no SELLER_CONTACT_EMAIL, no well-known file
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    assert r["fulfillment_status"] == "FAILED" and r["dispute"] is None and not _disputes(deps, r["purchase_id"])
    (err,) = _auto_failures(deps, r["purchase_id"])
    assert "no contact address" in err.payload["error"]


async def test_auto_dispute_second_call_on_the_same_purchase_files_nothing_new(deps, seller, contact_env):
    _auto(_with_contact(deps))
    first = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = first["purchase_id"]
    assert len(_disputes(deps, pid)) == 1
    second = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")  # same intent, same request
    assert second["purchase_id"] == pid and len(seller.paid) == 1  # no second payment
    assert {f["code"] for f in second["findings"]} == {"FULFILLMENT_FAILED", "DUPLICATE_SUSPECTED"}
    assert len(_disputes(deps, pid)) == 1 and not _auto_failures(deps, pid)
    assert [h["state"] for h in second["history"]].count("DISPUTE_FILED") == 1
    assert len(list((deps.artifact_dir / "disputes").iterdir())) == 1
    assert second["dispute"]["filed_by"] == "policy" and second["headline"].endswith("· dispute filed")
    assert second["safe_next_actions"] == ["human_review", "report_to_seller", "check_dispute"]


async def test_auto_dispute_covers_the_reconcile_path_for_paid_no_delivery(make_deps):
    seller = FakeSeller(mode="false_failure")  # settled on-chain, HTTP 402 body: PAID_NO_DELIVERY via reconcile_and_finish
    seller.support_email = "disputes@meridian.test"
    deps = _auto(make_deps(seller, chain_reader=used_on_chain))
    r = await purchase(deps, "intel", {"ticker": "NVDA"}, "demo-1")
    assert [f["code"] for f in r["findings"]] == ["PAID_NO_DELIVERY"] and r["payment_status"] == "SETTLED"
    assert r["dispute"]["filed_by"] == "policy" and r["dispute"]["to"] == "disputes@meridian.test"
    assert r["headline"].endswith("· dispute filed") and r["history"][-1]["state"] == "DISPUTE_FILED"
    (ev,) = _disputes(deps, r["purchase_id"])
    assert ev.payload["contact_source"] == "well_known" and ev.payload["finding_codes"] == ["PAID_NO_DELIVERY"]


async def test_auto_dispute_skips_when_the_call_budget_is_nearly_spent(deps, seller, contact_env, caplog):
    import logging

    from payguard.disputes import auto_dispute

    _auto(_with_contact(deps), on=False)
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    pid = r["purchase_id"]
    _auto(deps)
    row = deps.repo.get_purchase(pid)
    with caplog.at_level(logging.WARNING, logger="payguard.disputes"):
        same = await auto_dispute(deps, row, time.monotonic() + 2.0)  # < AUTO_MIN_REMAINING_S left
    assert same.payment_status == row.payment_status and not _disputes(deps, pid) and not _auto_failures(deps, pid)
    assert any("auto_dispute skipped" in rec.message for rec in caplog.records)
    after = await auto_dispute(deps, row, time.monotonic() + 45.0)  # the next terminal pass (e.g. reconcile) files it
    assert len(_disputes(deps, pid)) == 1 and after.history[-1]["state"] == "DISPUTE_FILED"
    assert await auto_dispute(deps, after, time.monotonic() + 45.0) is not None and len(_disputes(deps, pid)) == 1
