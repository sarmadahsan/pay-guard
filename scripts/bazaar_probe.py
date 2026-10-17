"""List CDP Bazaar sellers, keep cheap x402 v2 USDC sellers on Base mainnet, smoke-test candidates.

Usage:
  uv run python scripts/bazaar_probe.py                      # list candidates
  uv run python scripts/bazaar_probe.py --smoke 2            # pay the two cheapest with the bare SDK
  uv run python scripts/bazaar_probe.py --pick <resource_url> # print .env lines for M7

--smoke spends real mainnet USDC from BUYER_KEY_MAINNET.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from x402.client import x402Client
from x402.extensions.bazaar import ListDiscoveryResourcesParams, with_bazaar
from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
from x402.http.clients.httpx import PaymentError, x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
MAINNET = "eip155:8453"
PAGE_SIZE = 100
MAX_PAGES = 20
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
# Discovery lives on the CDP mainnet facilitator; x402.org (and FACILITATOR_URL in .env) is the testnet one with no /discovery route.
FACILITATOR_URL = os.environ.get("BAZAAR_FACILITATOR_URL", "https://api.cdp.coinbase.com/platform/v2/x402")


def _opt(accept: dict) -> dict | None:
    """Normalize one accepts entry from the discovery listing; None if malformed."""
    try:
        amount = int(accept.get("amount") or accept.get("maxAmountRequired") or 0)
    except (TypeError, ValueError):
        return None
    extra = accept.get("extra")
    return {
        "scheme": accept.get("scheme"),
        "network": accept.get("network"),
        "asset": str(accept.get("asset", "")).lower(),
        "pay_to": str(accept.get("payTo") or accept.get("pay_to") or "").lower(),
        "amount": amount,
        "extra": extra if isinstance(extra, dict) else {},
    }


def candidates(max_price_minor: int) -> list[dict]:
    client = with_bazaar(HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL)))
    items: list = []
    offset = 0
    for _ in range(MAX_PAGES):
        resp = client.extensions.bazaar.list_resources(
            ListDiscoveryResourcesParams(type="http", network=MAINNET, limit=PAGE_SIZE, offset=offset)
        )
        page = list(resp.items or [])
        items.extend(page)
        offset += len(page)
        total = getattr(getattr(resp, "pagination", None), "total", 0) or 0
        if not page or (total and offset >= total):
            break
    best: dict[str, dict] = {}
    for item in items:
        if item.x402_version != 2:
            continue
        for raw in item.accepts or []:
            if not isinstance(raw, dict):
                continue
            o = _opt(raw)
            if o is None:
                continue
            if o["scheme"] != "exact" or o["network"] != MAINNET or o["asset"] != USDC_MAINNET:
                continue
            if _is_permit2(o["extra"]):
                print(f"skip permit2 option: {item.resource}", file=sys.stderr)
                continue
            if o["amount"] <= 0 or o["amount"] > max_price_minor:
                continue
            cand = {
                "resource": item.resource,
                "amount": o["amount"],
                "pay_to": o["pay_to"],
                "extra": o["extra"],
                "mime_type": getattr(item, "mime_type", None),
            }
            if item.resource not in best or cand["amount"] < best[item.resource]["amount"]:
                best[item.resource] = cand
    return sorted(best.values(), key=lambda c: c["amount"])


def _is_permit2(extra: dict) -> bool:
    return str(extra.get("assetTransferMethod", "")).lower() == "permit2"


def pinned_policy(cand: dict):
    """Payment policy: only let the SDK sign the exact option vetted at listing time."""

    def policy(version: int, reqs: list) -> list:
        if version != 2:
            return []
        keep = []
        for r in reqs:
            try:
                ok = (
                    r.scheme == "exact"
                    and r.network == MAINNET
                    and str(r.asset).lower() == USDC_MAINNET
                    and str(r.pay_to).lower() == cand["pay_to"]
                    and not _is_permit2(r.extra or {})
                    and int(r.amount) <= cand["amount"]
                )
            except (AttributeError, TypeError, ValueError):
                ok = False
            if ok:
                keep.append(r)
        return keep

    return policy


def make_mainnet_client(account: Account, cand: dict) -> x402Client:
    client = x402Client()
    client.register(MAINNET, ExactEvmScheme(EthAccountSigner(account)))
    # Default spend cap is $1; the pinned policy below is the real cap (never more than the vetted amount).
    client.set_spend_controls({"max_amount_per_payment": False})
    client.register_policy(pinned_policy(cand))
    return client


async def smoke(cands: list[dict], n: int) -> None:
    key = os.environ.get("BUYER_KEY_MAINNET")
    if not key:
        print("BUYER_KEY_MAINNET is not set (put it in .env); cannot smoke.", file=sys.stderr)
        return
    acct = Account.from_key(key)
    chosen = cands[:n]
    print(f"mainnet buyer {acct.address}; planned total spend up to {sum(c['amount'] for c in chosen)} minor", file=sys.stderr)
    for c in chosen:
        try:
            async with x402HttpxClient(make_mainnet_client(acct, c), timeout=60) as http:
                r = await http.get(c["resource"])
            ct = r.headers.get("content-type", "")
            print(f"SMOKE {c['resource']} -> {r.status_code} {ct} settle_header={'PAYMENT-RESPONSE' in r.headers} body={r.text[:200]!r}")
        except PaymentError as e:
            print(f"SMOKE {c['resource']} -> PaymentError {e}")
        except Exception as e:  # noqa: BLE001
            print(f"SMOKE {c['resource']} -> {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-price-minor", type=int, default=100_000)
    ap.add_argument("--smoke", type=int, default=0)
    ap.add_argument("--pick", default=None)
    args = ap.parse_args()
    if args.smoke < 0:
        ap.error("--smoke must be >= 0")
    try:
        cands = candidates(args.max_price_minor)
    except Exception as e:  # noqa: BLE001
        print(f"LISTING FAILED ({type(e).__name__}): {str(e)[:300]}\nUse the written fallback from design §8.7 for M7.", file=sys.stderr)
        return 1
    print(f"{len(cands)} v2 USDC exact sellers on {MAINNET} at or below {args.max_price_minor} minor (permit2 excluded)")
    for c in cands[:25]:
        print(f"  {c['amount']:>8} minor  {c['resource']}  payTo={c['pay_to']}")
    if args.pick:
        match = next((c for c in cands if c["resource"] == args.pick), None)
        if not match:
            print("pick not in candidate list", file=sys.stderr)
            return 1
        print("\n# add to .env for M7")
        print(f"BAZAAR_URL={match['resource']}")
        print(f"BAZAAR_PAY_TO={match['pay_to']}")
        print(f"BAZAAR_PRICE_MINOR={match['amount']}")
        print(f"BAZAAR_CONTENT_TYPE={match.get('mime_type') or 'application/json'}")
        print("BAZAAR_TOOL_NAME=get_public_data")
    if args.smoke:
        asyncio.run(smoke(cands, args.smoke))
    return 0


if __name__ == "__main__":
    sys.exit(main())
