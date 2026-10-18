"""Hard-coded validators (design §5). Each returns (name, passed, detail). Exceptions mean UNVERIFIED.

`not_truncated` is the gate: it runs first for every service and, when it fails, run_validators stops there so a
later validator that merely trips over the cut body (pdf_page_count raising on a half PDF) cannot downgrade the
proven FULFILLMENT_FAILED into a suspected FULFILLMENT_UNVERIFIED.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
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

    raw = args["pages"]  # missing pages raises -> UNVERIFIED, never "seller sent != 0 pages"
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"pages must be a positive whole number, got {raw!r}")  # -> UNVERIFIED
    expected = raw
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
    if art.kind != "json":
        raise ValueError(f"{art.kind} artifact has no path to re-hash")  # -> UNVERIFIED
    return "content_hash", True, {"sha256": art.sha256, "recomputed": False}


GATE = "not_truncated"
_TEXTISH = re.compile(r"^(text/|application/(json|xml|javascript|x-www-form-urlencoded)$|application/.*\+(json|xml)$)")
_PDF_OBJ_AT = re.compile(rb"\s*(xref|\d+\s+\d+\s+obj)")


def _is_json_type(ct: str) -> bool:
    return ct == "application/json" or ct.endswith("+json")


def _raw_bytes(art: Artifact) -> bytes | None:
    """The delivered bytes, or None for an inline JSON artifact (which parsed, so it is structurally whole)."""
    return Path(art.path).read_bytes() if art.path else None


def _pdf_problems(raw: bytes) -> list[str]:
    problems: list[str] = []
    if not raw.startswith(b"%PDF-"):
        problems.append("no %PDF header")
    if not raw.rstrip(b" \t\r\n\x0c\x00").endswith(b"%%EOF"):
        problems.append("no %%EOF")
    pos = raw.rfind(b"startxref")
    if pos < 0:
        problems.append("no startxref")
    else:
        m = re.match(rb"startxref\s+(\d+)", raw[pos:pos + 40])
        offset = int(m.group(1)) if m else None
        if offset is None:
            problems.append("startxref has no offset")
        elif offset >= len(raw):
            problems.append(f"startxref offset {offset} beyond {len(raw)} bytes")
        elif not _PDF_OBJ_AT.match(raw, offset):
            problems.append(f"no xref at startxref offset {offset}")
    try:
        from pypdf import PdfReader

        len(PdfReader(io.BytesIO(raw), strict=False).pages)
    except Exception as e:  # noqa: BLE001  # pypdf's own exception zoo; any of them is evidence, never a crash
        problems.append(f"pypdf: {type(e).__name__}: {str(e).strip() or 'unreadable'}"[:160])
    return problems


def _json_cut(raw: bytes) -> str | None:
    """Why a non-parsing JSON body looks cut short (unbalanced brackets, open string), or None if it merely looks wrong."""
    stack: list[int] = []
    in_str = esc = False
    pairs = {ord("{"): ord("}"), ord("["): ord("]")}
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:  # quote
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in pairs:
            stack.append(pairs[b])
        elif b in (0x7D, 0x5D):  # } ]
            if not stack or stack[-1] != b:
                return None  # mismatched close: malformed, not merely cut
            stack.pop()
    if in_str:
        return "unterminated string"
    if stack:
        return f"unbalanced brackets ({len(stack)} open)"
    return None


def _text_problems(raw: bytes) -> list[str]:
    problems: list[str] = []
    if raw.endswith(b"\x00"):
        problems.append(f"trailing NUL padding ({len(raw) - len(raw.rstrip(b'\x00'))} bytes)")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start >= len(raw) - 3 and "unexpected end" in e.reason:
            problems.append(f"cut inside a UTF-8 multibyte sequence at byte {e.start}")
    return problems


def not_truncated(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    """Content-independent: was the whole deliverable received? Returns False (never raises) on every detected cut."""
    received = art.received_length if art.received_length is not None else art.size
    declared = art.declared_length
    detail: dict[str, Any] = {"declared_length": declared, "received_length": received, "checks": {}}
    checks = detail["checks"]
    problems: list[str] = []
    label = "body"
    if declared is not None and declared != received:
        checks["transport"] = f"{received} of {declared} declared bytes"
        problems.append(checks["transport"])
    elif declared is not None:
        checks["transport"] = "length matches"
    else:
        checks["transport"] = "no Content-Length"
    raw = _raw_bytes(art)
    ct = art.content_type
    malformed: str | None = None
    if art.kind == "pdf" or ct == "application/pdf":
        label = "pdf"
        pdf = _pdf_problems(raw or b"")
        checks["pdf"] = pdf or "intact"
        problems = pdf + problems  # structural reason first, transport count last
    elif _is_json_type(ct):
        label = "json"
        if art.kind == "json":
            checks["json"] = "parses"
        else:
            raw = raw or b""
            try:
                json.loads(raw)
                checks["json"] = "parses"
            except (ValueError, RecursionError) as e:  # same pair store_artifact catches; a nested bomb must not raise
                cut = _json_cut(raw) if raw.strip() else "empty body"
                if cut:
                    checks["json"] = cut
                    problems = [cut] + problems
                else:
                    malformed = f"malformed json ({e})"[:160]
                    checks["json"] = malformed
    if raw is not None and _TEXTISH.match(ct):  # text-like only: zip/gzip/tar legitimately end in NUL bytes
        text = _text_problems(raw)
        if text:
            checks["text"] = text
            problems = problems + text
    if malformed and not problems:
        detail["reason"] = malformed  # does not parse, but nothing says it was cut: a seller bug, still a failed delivery
        return GATE, False, detail
    if problems:
        detail["reason"] = f"{label} truncated ({'; '.join(problems)})"
        return GATE, False, detail
    return GATE, True, detail


VALIDATORS: dict[str, Validator] = {
    "not_truncated": not_truncated,
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
    results: list[ValidatorResult] = []
    for n in service.validators:
        r = resolve(n)(service, args, artifact)
        results.append(r)
        if n == GATE and not r[1]:
            break  # a cut body proves nothing further; stop before a later validator raises on it
    return results
