"""Pay each demo seller with the plain x402 SDK client. No PayGuard code on this path (metric 1).

Usage:
  uv run python scripts/smoke_sellers.py
  uv run python scripts/smoke_sellers.py --naive-retry   # pays /research-slow twice with the bare SDK
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from pypdf import PdfReader
from x402.client import x402Client
from x402.http.clients.httpx import PaymentError, x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
BASE = os.environ.get("SELLER_BASE_URL", "http://127.0.0.1:8402")


def make_client() -> x402Client:
    acct = Account.from_key(os.environ["BUYER_KEY_SEPOLIA"])
    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(EthAccountSigner(acct)))
    client.set_spend_controls({"max_amount_per_payment": False})  # SDK default cap is $1; /convert is $2
    print(f"buyer {acct.address}")
    return client


async def buy(http: x402HttpxClient, method: str, url: str, **kw):
    t0 = time.monotonic()
    resp = await http.request(method, url, **kw)
    dt = time.monotonic() - t0
    settled = "PAYMENT-RESPONSE" in resp.headers
    print(f"{method} {url} -> {resp.status_code} in {dt:.1f}s settle_header={'yes' if settled else 'no'} bytes={len(resp.content)}")
    return resp


async def main(naive_retry: bool) -> int:
    async with x402HttpxClient(make_client(), timeout=60) as http:
        r = await buy(http, "GET", f"{BASE}/report/ACME")
        assert r.status_code == 200, f"/report failed: {r.status_code} {r.text[:200]}"
        assert r.json()["ticker"] == "ACME"
        assert "PAYMENT-RESPONSE" in r.headers, "/report settled without PAYMENT-RESPONSE"

        r = await buy(http, "POST", f"{BASE}/convert", json={"doc_id": "d1", "pages": 20})
        assert r.status_code == 200, f"/convert failed: {r.status_code} {r.text[:200]}"
        assert r.headers["content-type"].startswith("application/pdf")
        assert len(PdfReader(io.BytesIO(r.content)).pages) > 0, "/convert returned a PDF with no pages"
        assert "PAYMENT-RESPONSE" in r.headers, "/convert settled without PAYMENT-RESPONSE"

        if naive_retry:
            print("naive retry: the bare SDK client pays, times out on the stall, and pays again")
            for attempt in (1, 2):
                try:
                    r = await buy(http, "GET", f"{BASE}/research-slow", params={"topic": "naive"})
                    print(f"attempt {attempt}: {r.status_code}")
                    assert r.status_code == 200, f"/research-slow failed: {r.status_code} {r.text[:200]}"
                except PaymentError as e:
                    print(f"attempt {attempt}: PaymentError after paying: {e}")
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--naive-retry", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().naive_retry)))
