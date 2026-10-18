"""sellers/truncate.py: the opt-in truncation switch of the demo seller (no payguard import, off by default)."""
from __future__ import annotations

import io

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from sellers.truncate import RATIO, TruncateMiddleware, cut_length, install_truncation, truncate_paths

SETTLED = {"PAYMENT-RESPONSE": "eyJzdWNjZXNzIjp0cnVlfQ=="}  # any value: the middleware only checks presence


def pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_app(paths) -> FastAPI:
    app = FastAPI()

    @app.get("/report/{ticker}")
    async def report(ticker: str):
        return JSONResponse({"ticker": ticker, "name": "x" * 50}, headers=SETTLED)

    @app.post("/convert")
    async def convert():
        return Response(content=pdf(12), media_type="application/pdf", headers=SETTLED)

    @app.get("/unpaid")
    async def unpaid():
        return JSONResponse({"free": True})  # 2xx without a settlement header: never cut

    @app.get("/report-402/{ticker}")
    async def denied(ticker: str):
        return JSONResponse({"error": "payment required"}, status_code=402, headers=SETTLED)

    install_truncation(app, paths)
    return app


def test_truncate_paths_parsing(monkeypatch):
    assert truncate_paths("") == ()
    assert truncate_paths("/convert") == ("/convert",)
    assert truncate_paths(" /convert , /report,,junk, /convert") == ("/convert", "/report")
    monkeypatch.setenv("SELLER_TRUNCATE_PATHS", "/report")
    assert truncate_paths() == ("/report",)
    monkeypatch.delenv("SELLER_TRUNCATE_PATHS")
    assert truncate_paths() == ()


def test_cut_length():
    assert cut_length(0) == 0 and cut_length(1) == 1 and cut_length(1767) == int(1767 * RATIO) == 1060


def test_unset_installs_nothing(monkeypatch):
    monkeypatch.delenv("SELLER_TRUNCATE_PATHS", raising=False)
    app = FastAPI()
    assert install_truncation(app) == () and app.user_middleware == []


def test_settled_pdf_on_matching_path_is_cut_with_matching_content_length():
    full = pdf(12)
    client = TestClient(make_app(("/convert",)))
    r = client.post("/convert")
    assert r.status_code == 200 and "PAYMENT-RESPONSE" in r.headers
    assert len(r.content) == cut_length(len(full)) < len(full)
    assert r.headers["content-length"] == str(len(r.content))  # structurally short, transport-consistent
    assert not r.content.rstrip().endswith(b"%%EOF")
    try:
        PdfReader(io.BytesIO(r.content)).pages[0]
        pypdf_ok = True
    except Exception:  # noqa: BLE001
        pypdf_ok = False
    assert not pypdf_ok or len(r.content) < len(full)  # either way the buyer got less than it paid for


def test_settled_json_on_matching_prefix_is_cut():
    client = TestClient(make_app(("/report",)))
    r = client.get("/report/ACME")
    assert r.status_code == 200 and r.headers["content-length"] == str(len(r.content))
    assert r.content.startswith(b'{"ticker":"ACME"') and not r.content.endswith(b"}")


def test_other_paths_unpaid_and_402_are_untouched():
    client = TestClient(make_app(("/convert",)))
    r = client.get("/report/ACME")
    assert r.json() == {"ticker": "ACME", "name": "x" * 50}
    client = TestClient(make_app(("/report", "/unpaid")))
    assert client.get("/unpaid").json() == {"free": True}
    r = client.get("/report-402/ACME")
    assert r.status_code == 402 and r.json() == {"error": "payment required"}


def test_middleware_passes_non_http_scopes_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = TruncateMiddleware(inner, ("/convert",))
    import asyncio
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_sellers_import_nothing_from_payguard():
    import pathlib
    src = pathlib.Path("sellers/truncate.py").read_text()
    assert "payguard" not in src.replace("the guard package", "")
