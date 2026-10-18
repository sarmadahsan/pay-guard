"""not_truncated: a cut-short deliverable is a proven FULFILLMENT_FAILED, for PDFs and JSON, with both byte lengths."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from payguard.artifacts import Artifact, store_artifact
from payguard.models import EvidenceKind
from payguard.outcome import Outcome
from payguard.purchase import purchase
from payguard.registry import load_registry
from payguard.verify import GATE, not_truncated, run_validators
from tests.conftest import FakeSeller, pdf_bytes

REPORT = {"ticker": "ACME", "name": "Acme", "revenue_usd": 5, "fiscal_year": 2025, "summary": "héllo wörld"}


@pytest.fixture
def reg():
    return load_registry(base_url="http://seller.test")


def cut(b: bytes, ratio: float = 0.6) -> bytes:
    return b[: int(len(b) * ratio)]


# ---- registry / runner ------------------------------------------------------------------------------------------

def test_gate_is_first_for_every_service(reg, monkeypatch):
    monkeypatch.setenv("INTEL_BASE_URL", "http://meridian.test")
    monkeypatch.setenv("BAZAAR_URL", "http://bazaar.test/x")
    monkeypatch.setenv("BAZAAR_PRICE_MINOR", "10000")
    full = load_registry(base_url="http://seller.test")
    assert set(full) == {"report", "report_premium", "research", "convert", "intel", "bazaar"}
    assert all(svc.validators[0] == GATE for svc in full.values())


def test_runner_stops_after_gate_failure_so_pdf_page_count_cannot_raise(reg, tmp_path):
    art = store_artifact(cut(pdf_bytes(12)), "application/pdf", "p1", tmp_path)
    svc = replace(reg["convert"], validators=("not_truncated", "pdf_page_count"))
    results = run_validators(svc, {}, art)  # pages missing -> pdf_page_count would raise KeyError
    assert [r[0] for r in results] == ["not_truncated"] and results[0][1] is False
    assert results[0][2]["reason"].startswith("pdf truncated (no %%EOF")


def test_runner_runs_everything_when_gate_passes(reg, tmp_path):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p1", tmp_path, declared_length=len(pdf_bytes(12)))
    results = run_validators(reg["convert"], {"pages": 12}, art)
    assert [r[0] for r in results] == ["not_truncated", "content_type", "non_empty", "pdf_page_count"]
    assert all(r[1] for r in results) and results[0][2]["checks"] == {"transport": "length matches", "pdf": "intact"}


# ---- (a) transport ----------------------------------------------------------------------------------------------

def test_transport_mismatch_fails_even_when_structure_is_fine(reg, tmp_path):
    body = json.dumps(REPORT).encode()
    art = store_artifact(body, "application/json", "p1", tmp_path, declared_length=len(body) + 40)
    name, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"] == f"json truncated ({len(body)} of {len(body) + 40} declared bytes)"
    assert d["declared_length"] == len(body) + 40 and d["received_length"] == len(body)


def test_no_content_length_is_not_a_failure(reg, tmp_path):
    art = store_artifact(json.dumps(REPORT).encode(), "application/json", "p1", tmp_path)
    assert not_truncated(reg["report"], {}, art)[1] is True


def test_outcome_declared_length_ignores_encoded_and_garbage_values():
    assert Outcome("response", 200, {"content-length": "1767"}).declared_length() == 1767
    assert Outcome("response", 200, {"content-length": "1767", "content-encoding": "gzip"}).declared_length() is None
    assert Outcome("response", 200, {"content-length": "abc"}).declared_length() is None
    assert Outcome("response", 200, {}).declared_length() is None


# ---- (b) pdf ----------------------------------------------------------------------------------------------------

def test_cut_pdf_reports_eof_xref_and_pypdf(reg, tmp_path):
    full = pdf_bytes(12)
    art = store_artifact(cut(full), "application/pdf", "p1", tmp_path, declared_length=len(full))
    name, ok, d = not_truncated(reg["convert"], {"pages": 12}, art)
    assert name == GATE and ok is False
    assert d["reason"].startswith("pdf truncated (no %%EOF; no startxref; pypdf: ")
    assert d["reason"].endswith(f"; {len(cut(full))} of {len(full)} declared bytes)")
    assert art.page_count is None  # today's signal; the gate turns it into a proven failure instead of a None compare


def test_pdf_missing_only_eof_marker(reg, tmp_path):
    full = pdf_bytes(2)
    art = store_artifact(full.rstrip()[: -len(b"%%EOF")], "application/pdf", "p1", tmp_path)
    _, ok, d = not_truncated(reg["convert"], {}, art)
    assert ok is False and "no %%EOF" in d["checks"]["pdf"]


def test_pdf_with_bad_startxref_offset(reg, tmp_path):
    full = pdf_bytes(2)
    pos = full.rfind(b"startxref")
    bad = full[:pos] + b"startxref\n999999\n%%EOF\n"
    art = store_artifact(bad, "application/pdf", "p1", tmp_path)
    _, ok, d = not_truncated(reg["convert"], {}, art)
    assert ok is False and any("startxref offset 999999 beyond" in c for c in d["checks"]["pdf"])


def test_pdf_with_trailing_whitespace_after_eof_is_intact(reg, tmp_path):
    art = store_artifact(pdf_bytes(3) + b"\r\n\n", "application/pdf", "p1", tmp_path)
    assert not_truncated(reg["convert"], {}, art)[1] is True


def test_garbage_pdf_never_raises(reg, tmp_path):
    art = store_artifact(b"%PDF-1.4 not really", "application/pdf", "p1", tmp_path)
    _, ok, d = not_truncated(reg["convert"], {}, art)
    assert ok is False and d["reason"].startswith("pdf truncated (")


# ---- (c) json ---------------------------------------------------------------------------------------------------

def test_json_cut_mid_array_is_truncated(reg, tmp_path):
    body = json.dumps({"topic": "t", "summary": "s", "sources": ["https://a", "https://b"]}).encode()
    cut_at = body.index(b'"https://b"') - 1  # right after the first array element's comma
    art = store_artifact(body[:cut_at], "application/json", "p1", tmp_path)
    assert art.kind == "bytes"  # did not parse, so the raw bytes went to disk
    _, ok, d = not_truncated(reg["research"], {}, art)
    assert ok is False and d["reason"] == "json truncated (unbalanced brackets (2 open))"


def test_json_cut_mid_string_is_truncated(reg, tmp_path):
    body = json.dumps(REPORT).encode()
    art = store_artifact(body[: body.index(b"Acme") + 2], "application/json", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"] == "json truncated (unterminated string)"


def test_json_balanced_but_invalid_is_malformed_not_truncated(reg, tmp_path):
    art = store_artifact(b'{"a": 1,}', "application/json", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"].startswith("malformed json (") and "truncated" not in d["reason"]


def test_json_mismatched_close_is_malformed(reg, tmp_path):
    art = store_artifact(b'{"a": [1, 2}', "application/json", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"].startswith("malformed json (")


def test_json_empty_body_is_truncated(reg, tmp_path):
    art = store_artifact(b"", "application/json", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"] == "json truncated (empty body)"


def test_inline_json_artifact_passes(reg):
    art = Artifact("json", "application/json", "0" * 64, 2, json={"a": 1}, received_length=2)
    assert not_truncated(reg["report"], {}, art)[1] is True


# ---- (d) text-ish -----------------------------------------------------------------------------------------------

def test_trailing_nul_padding(reg, tmp_path):
    art = store_artifact(b"hello world\x00\x00\x00", "text/plain", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"] == "body truncated (trailing NUL padding (3 bytes))"


def test_cut_inside_utf8_multibyte(reg, tmp_path):
    body = "héllo wö".encode()[:-1]  # drops the second byte of ö
    art = store_artifact(body, "text/plain", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"] == f"body truncated (cut inside a UTF-8 multibyte sequence at byte {len(body) - 1})"


def test_json_cut_inside_multibyte_reports_both(reg, tmp_path):
    body = json.dumps(REPORT, ensure_ascii=False).encode()
    body = body[: body.index("ö".encode()) + 1]
    art = store_artifact(body, "application/json", "p1", tmp_path)
    _, ok, d = not_truncated(reg["report"], {}, art)
    assert ok is False and d["reason"].startswith("json truncated (unterminated string; cut inside a UTF-8")


def test_invalid_utf8_in_the_middle_is_not_truncation(reg, tmp_path):
    art = store_artifact(b"he\xffllo", "text/plain", "p1", tmp_path)
    assert not_truncated(reg["report"], {}, art)[1] is True


# ---- evidence ---------------------------------------------------------------------------------------------------

def test_artifact_evidence_round_trips_lengths_and_tolerates_old_rows(tmp_path):
    art = store_artifact(pdf_bytes(1), "application/pdf", "p1", tmp_path, declared_length=99)
    ev = art.evidence()
    assert ev["declared_length"] == 99 and ev["received_length"] == art.size
    assert Artifact.from_evidence(ev) == art and art.summary()["declared_length"] == 99
    old = {k: v for k, v in ev.items() if k not in ("declared_length", "received_length")}
    back = Artifact.from_evidence(old)
    assert back.declared_length is None and back.received_length is None


# ---- end to end -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("service, args, label, kind", [
    ("convert", {"doc_id": "d1", "pages": 12}, "pdf", "pdf"),
    ("report", {"ticker": "ACME"}, "json", "bytes"),
])
async def test_cut_delivery_is_settled_and_proven_failed(make_deps, service, args, label, kind):
    seller = FakeSeller(mode="truncated")
    deps = make_deps(seller)
    r = await purchase(deps, service, args, f"intent-{service}", "task")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "FAILED"
    assert r["settlement_tx"] == seller.tx and len(seller.paid) == 1
    codes = [f["code"] for f in r["findings"]]
    assert codes == ["FULFILLMENT_FAILED"], codes  # one proven finding; no FULFILLMENT_UNVERIFIED masking it
    f = r["findings"][0]
    assert f["severity"] == "proven" and f["safe_next_actions"] == ["human_review", "report_to_seller"]
    assert f["summary"].startswith(f"Validator 'not_truncated' failed: {label} truncated (")
    art = r["artifact"]
    assert art["kind"] == kind and art["declared_length"] > art["received_length"] == art["size"]
    assert f["summary"].endswith(f"; {art['received_length']} of {art['declared_length']} declared bytes)")
    evid = {e.evidence_id: e for e in deps.repo.list_evidence(r["purchase_id"])}
    kinds = {evid[i].kind for i in f["evidence_ids"]}
    assert kinds == {EvidenceKind.VALIDATOR, EvidenceKind.ARTIFACT}
    val = [evid[i] for i in f["evidence_ids"] if evid[i].kind == EvidenceKind.VALIDATOR][0]
    assert val.payload["name"] == "not_truncated" and val.payload["passed"] is False and "checks" in val.payload
    artifact_ev = [evid[i] for i in f["evidence_ids"] if evid[i].kind == EvidenceKind.ARTIFACT][0]
    assert artifact_ev.payload["declared_length"] == art["declared_length"]
    assert [e.kind for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == EvidenceKind.VALIDATOR] == [EvidenceKind.VALIDATOR]
    assert not any("pay" in a or "retry" in a for a in r["safe_next_actions"])


async def test_intact_convert_shows_matching_lengths(deps):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 12}, "intent-ok", "task")
    assert r["fulfillment_status"] == "VERIFIED" and r["findings"] == []
    art = r["artifact"]
    assert art["declared_length"] == art["received_length"] == art["size"] > 0
