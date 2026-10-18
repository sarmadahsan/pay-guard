"""Demo x402 sellers. Official x402 middleware only. ZERO imports from the guard package (PRD F11, metric 1).

Routes (design §4):
  GET  /report/{ticker}          $0.50  JSON company record
  GET  /research-slow?topic=     $0.75  JSON research result (M5 adds the after-settle stall hook)
  POST /convert {doc_id, pages}  $2.00  PDF, always 12 pages
  GET  /report-premium/{ticker}  $0.75  same as /report (registry accepts $0.50 -> PRICE_MISMATCH)
"""
from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pypdf import PdfWriter
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

from sellers.stall import make_stall_hook

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("sellers")

NETWORK = "eip155:84532"
PAY_TO = os.environ["SELLER_PAY_TO"]
CONVERT_PAGES = 12  # always short-delivers (scenario C)


def redact_url(url: str) -> str:
    """scheme://host[:port]/path only: drops user-info and query string so no credential is logged."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


def build_server() -> x402ResourceServer:
    mode = os.environ.get("SELLER_FACILITATOR", "remote")
    if mode == "local":
        from sellers.local_facilitator import build_local_facilitator

        server = x402ResourceServer(build_local_facilitator())
        log.info("facilitator: local in-process")
    else:
        url = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")
        server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=url)))
        log.info("facilitator: remote %s", redact_url(url))
    server.register(NETWORK, ExactEvmServerScheme())
    return server


def option(price: str) -> PaymentOption:
    return PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=NETWORK, max_timeout_seconds=300)


ROUTES = {
    "GET /report/*": RouteConfig(accepts=option("$0.50"), description="Company report", mime_type="application/json"),
    "GET /report-premium/*": RouteConfig(accepts=option("$0.75"), description="Premium company report", mime_type="application/json"),
    "GET /research-slow": RouteConfig(accepts=option("$0.75"), description="Research report (slow)", mime_type="application/json"),
    "POST /convert": RouteConfig(accepts=option("$2.00"), description="Document conversion", mime_type="application/pdf"),
}

server = build_server()
server.on_after_settle(make_stall_hook(float(os.environ.get("SELLER_STALL_S", "90"))))
app = FastAPI(title="PayGuard demo sellers")
_middleware = payment_middleware(ROUTES, server)


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    return await _middleware(request, call_next)


def company_record(ticker: str) -> dict:
    t = ticker.upper()
    seed = sum(ord(c) for c in t)
    return {
        "ticker": t,
        "name": f"{t.title()} Holdings Inc.",
        "revenue_usd": 1_000_000 * (seed % 97 + 3),
        "fiscal_year": 2025,
        "summary": f"{t} is a demo company record served by an ordinary x402 seller.",
    }


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "network": NETWORK, "pay_to": PAY_TO}


@app.get("/report/{ticker}")
async def report(ticker: str) -> JSONResponse:
    return JSONResponse(company_record(ticker))


@app.get("/report-premium/{ticker}")
async def report_premium(ticker: str) -> JSONResponse:
    rec = company_record(ticker)
    rec["tier"] = "premium"
    return JSONResponse(rec)


@app.get("/research-slow")
async def research_slow(topic: str = "untitled") -> JSONResponse:
    return JSONResponse(
        {
            "topic": topic,
            "summary": f"Three findings about {topic}, served after settlement.",
            "sources": ["https://example.com/a", "https://example.com/b"],
        }
    )


@app.post("/convert")
async def convert(body: dict) -> Response:
    writer = PdfWriter()
    for _ in range(CONVERT_PAGES):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    log.info("convert: requested pages=%s delivered=%s", body.get("pages"), CONVERT_PAGES)
    return Response(content=buf.getvalue(), media_type="application/pdf")
