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
