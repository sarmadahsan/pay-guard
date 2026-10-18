"""Hard-coded validators (design §5). Each returns (name, passed, detail). Exceptions mean UNVERIFIED."""
from __future__ import annotations

from typing import Any, Callable

import jsonschema

from payguard.artifacts import Artifact
from payguard.registry import Service

ValidatorResult = tuple[str, bool, dict[str, Any]]
Validator = Callable[[Service, dict[str, Any], Artifact], ValidatorResult]

SCHEMAS: dict[str, dict[str, Any]] = {
    "company_report": {
        "type": "object",
        "required": ["ticker", "name", "revenue_usd", "fiscal_year"],
        "properties": {
            "ticker": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "revenue_usd": {"type": "number"},
            "fiscal_year": {"type": "integer"},
        },
    },
    "research_report": {
        "type": "object",
        "required": ["topic", "summary", "sources"],
        "properties": {
            "topic": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "sources": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        },
    },
}


def json_schema(schema_name: str) -> Validator:
    schema = SCHEMAS[schema_name]  # KeyError on purpose for unknown schema names

    def _v(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
        name = f"json_schema:{schema_name}"
        if art.kind != "json":
            return name, False, {"reason": f"artifact is {art.kind}, not json"}
        try:
            jsonschema.validate(art.json, schema)
        except jsonschema.ValidationError as e:
            return name, False, {"reason": e.message, "path": list(e.absolute_path)}
        return name, True, {"schema": schema_name}

    return _v


def ticker_matches(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    expected = str(args.get("ticker") or "").upper()
    raw = art.json.get("ticker") if art.kind == "json" and isinstance(art.json, dict) else None
    actual = raw.upper() if isinstance(raw, str) else ""
    ok = bool(expected) and expected == actual
    return "ticker_matches", ok, {"expected": expected, "actual": actual, **({} if ok else {"reason": f"ticker {actual!r} != requested {expected!r}"})}


def content_type(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    ok = art.content_type == service.expected_content_type
    return "content_type", ok, {"expected": service.expected_content_type, "actual": art.content_type,
                                **({} if ok else {"reason": f"content-type {art.content_type!r} != {service.expected_content_type!r}"})}


def non_empty(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    ok = art.size > 0 and (art.kind != "json" or art.json not in (None, {}, [], ""))
    return "non_empty", ok, {"size": art.size, **({} if ok else {"reason": "empty artifact"})}


def pdf_page_count(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    from payguard import artifacts as _artifacts

    expected = int(args.get("pages", 0))
    actual = art.page_count if art.page_count is not None else (_artifacts.pdf_page_count(art.path) if art.path else None)
    ok = actual is not None and actual == expected
    detail: dict[str, Any] = {"expected": expected, "actual": actual}
    if not ok:
        detail["reason"] = f"page count {actual} != requested {expected}"
    return "pdf_page_count", ok, detail


def content_hash(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    import hashlib
    from pathlib import Path

    if art.path:
        actual = hashlib.sha256(Path(art.path).read_bytes()).hexdigest()
        ok = actual == art.sha256
        return "content_hash", ok, {"sha256": art.sha256, "recomputed": True, **({} if ok else {"reason": f"content hash {actual} != recorded {art.sha256}"})}
    return "content_hash", True, {"sha256": art.sha256, "recomputed": False}


VALIDATORS: dict[str, Validator] = {
    "ticker_matches": ticker_matches,
    "content_type": content_type,
    "non_empty": non_empty,
    "pdf_page_count": pdf_page_count,
    "content_hash": content_hash,
}


def resolve(name: str) -> Validator:
    if name.startswith("json_schema:"):
        return json_schema(name.split(":", 1)[1])
    return VALIDATORS[name]  # KeyError -> caller records FULFILLMENT_UNVERIFIED


def run_validators(service: Service, args: dict[str, Any], artifact: Artifact) -> list[ValidatorResult]:
    return [resolve(n)(service, args, artifact) for n in service.validators]
