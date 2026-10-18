import json
from pathlib import Path

import pytest

from payguard.artifacts import Artifact, store_artifact
from payguard.registry import load_registry
from payguard.verify import run_validators


@pytest.fixture
def reg():
    return load_registry(base_url="http://seller.test")


def json_art(obj) -> Artifact:
    return store_artifact(json.dumps(obj).encode(), "application/json", "p1", Path("/tmp/unused"))


def test_store_json_artifact_inline(tmp_path):
    art = store_artifact(b'{"a": 1}', "application/json; charset=utf-8", "p1", tmp_path)
    assert art.kind == "json" and art.json == {"a": 1} and art.path is None and len(art.sha256) == 64


def test_store_binary_artifact_as_path(tmp_path):
    art = store_artifact(b"%PDF-1.4 fake", "application/pdf", "p2", tmp_path)
    assert art.kind == "pdf" and art.path == str(tmp_path / "p2.pdf") and Path(art.path).read_bytes().startswith(b"%PDF")
    assert art.evidence()["path"] == art.path and "json" in art.evidence()


def test_report_validators_pass(reg):
    art = json_art({"ticker": "ACME", "name": "Acme", "revenue_usd": 5, "fiscal_year": 2025, "summary": "s"})
    results = run_validators(reg["report"], {"ticker": "ACME"}, art)
    assert [r[0] for r in results] == ["content_type", "non_empty", "json_schema:company_report", "ticker_matches"]
    assert all(r[1] for r in results)


def test_ticker_mismatch_fails(reg):
    art = json_art({"ticker": "OTHER", "name": "x", "revenue_usd": 1, "fiscal_year": 2025, "summary": "s"})
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["ticker_matches"][1] is False and "OTHER" in results["ticker_matches"][2]["reason"]


def test_schema_failure_has_reason(reg):
    art = json_art({"ticker": "ACME"})
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["json_schema:company_report"][1] is False and "reason" in results["json_schema:company_report"][2]


def test_content_type_and_non_empty(reg, tmp_path):
    art = store_artifact(b"", "text/plain", "p3", tmp_path)
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["content_type"][1] is False and results["non_empty"][1] is False


def test_unknown_validator_raises(reg):
    from dataclasses import replace
    svc = replace(reg["report"], validators=("does_not_exist",))
    with pytest.raises(KeyError):
        run_validators(svc, {}, json_art({}))


@pytest.mark.parametrize("body", [b"[" * 100000 + b"]" * 100000, b'{"a":' + b"9" * 5000 + b"}"], ids=["deep_nesting", "huge_int"])
def test_hostile_json_falls_back_to_bytes(tmp_path, body):
    art = store_artifact(body, "application/json", "p9", tmp_path)
    assert art.kind == "bytes" and art.path


def test_list_body_does_not_abort_validators(reg):
    art = json_art([1, 2])
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["ticker_matches"][1] is False and results["json_schema:company_report"][1] is False


def test_none_ticker_does_not_match_none_string(reg):
    art = json_art({"ticker": "None", "name": "x", "revenue_usd": 1, "fiscal_year": 2025})
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": None}, art))
    assert results["ticker_matches"][1] is False


def test_content_type_requires_equality(reg):
    art = json_art({"a": 1})
    art.content_type = "application/jsonx"
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["content_type"][1] is False


def test_conflicting_store_does_not_overwrite_earlier_artifact(tmp_path):
    first = store_artifact(b"%PDF-1.4 one", "application/pdf", "p5", tmp_path)
    second = store_artifact(b"%PDF-1.4 two", "application/pdf", "p5", tmp_path)
    assert first.path != second.path
    assert Path(first.path).read_bytes() == b"%PDF-1.4 one" and Path(second.path).read_bytes() == b"%PDF-1.4 two"
    again = store_artifact(b"%PDF-1.4 one", "application/pdf", "p5", tmp_path)
    assert again.path == first.path


def test_null_json_ticker_does_not_match_none_string(reg):
    art = json_art({"ticker": None, "name": "x", "revenue_usd": 1, "fiscal_year": 2025})
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "None"}, art))
    assert results["ticker_matches"][1] is False


@pytest.mark.parametrize("pid", ["../escape", "a/b", "", ".."])
def test_store_rejects_purchase_id_outside_root(tmp_path, pid):
    with pytest.raises(ValueError):
        store_artifact(b"%PDF-1.4", "application/pdf", pid, tmp_path / "root")


# ---- M4: pdf_page_count and content_hash ----
from payguard.purchase import purchase
from tests.conftest import pdf_bytes


def test_pdf_page_count_validator(reg, tmp_path):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p9", tmp_path)
    results = dict((r[0], r) for r in run_validators(reg["convert"], {"doc_id": "d", "pages": 20}, art))
    name, passed, detail = results["pdf_page_count"]
    assert passed is False and detail["expected"] == 20 and detail["actual"] == 12 and "12" in detail["reason"] and "20" in detail["reason"]
    ok = dict((r[0], r) for r in run_validators(reg["convert"], {"doc_id": "d", "pages": 12}, art))
    assert ok["pdf_page_count"][1] is True


def test_pdf_page_count_without_pages_arg_raises(reg, tmp_path):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p11", tmp_path)
    with pytest.raises(KeyError):
        run_validators(reg["convert"], {"doc_id": "d"}, art)


@pytest.mark.parametrize("pages", [12.9, True, 0, -1, "abc", None])
def test_pdf_page_count_rejects_invalid_pages_arg(reg, tmp_path, pages):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p12", tmp_path)
    with pytest.raises(ValueError):
        run_validators(reg["convert"], {"doc_id": "d", "pages": pages}, art)


def test_content_hash_pathless_file_artifact_raises_inline_json_passes():
    from dataclasses import replace
    from payguard.verify import content_hash
    svc = replace(load_registry(base_url="http://x")["report"], validators=("content_hash",))
    with pytest.raises(ValueError):
        content_hash(svc, {}, Artifact("pdf", "application/pdf", "0" * 64, 10))
    art = json_art({"a": 1})
    assert content_hash(svc, {}, art) == ("content_hash", True, {"sha256": art.sha256, "recomputed": False})


def test_content_hash_validator(tmp_path):
    from dataclasses import replace
    from payguard.verify import content_hash
    svc = replace(load_registry(base_url="http://x")["report"], validators=("content_hash",))
    art = store_artifact(b"hello", "text/plain", "p10", tmp_path)
    name, passed, detail = content_hash(svc, {}, art)
    assert passed and detail["sha256"] == art.sha256 and detail["recomputed"] is True
    Path(art.path).write_bytes(b"tampered")
    assert content_hash(svc, {}, art)[1] is False


async def test_convert_flow_fulfillment_failed_with_evidence(deps, seller):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "FAILED"
    assert r["artifact"]["path"].endswith(".pdf") and r["artifact"]["page_count"] == 12 and "json" not in r["artifact"]
    f = [x for x in r["findings"] if x["code"] == "FULFILLMENT_FAILED"]
    assert len(f) == 1 and f[0]["severity"] == "proven" and "12" in f[0]["summary"] and "20" in f[0]["summary"]
    kinds = {e.evidence_id: e.kind for e in deps.repo.list_evidence(r["purchase_id"])}
    assert {kinds[i] for i in f[0]["evidence_ids"]} == {"validator", "artifact"}
    assert r["safe_next_actions"] == ["human_review", "report_to_seller"]
    assert len(seller.paid) == 1 and deps.repo.spent_minor("test") == 2_000_000
