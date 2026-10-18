"""Dump PayGuard's MCP tool surface (names, descriptions, JSON schemas) to mcp-tools.json for registry listings.

Read-only: it imports the server module and lists its tools; it never starts the server, opens a wallet or pays.

The two env-gated tools (Meridian Intel and the Bazaar seller) are registered at import time only when their env
variables are set, so this script sets obviously fake placeholder values first (overriding any operator .env) to make
the export show the full surface. It refuses to write anything if an output string looks like a private key.

    uv run python scripts/export_tool_schema.py            # writes mcp-tools.json at the repo root
    uv run python scripts/export_tool_schema.py --check    # exit 1 if the committed mcp-tools.json is stale
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mcp-tools.json"

# Placeholders only: `.invalid` never resolves (RFC 2606) and the pay-to is the burn address. Set before the import.
PLACEHOLDER_ENV = {
    "INTEL_BASE_URL": "http://intel.placeholder.invalid",
    "BAZAAR_URL": "https://bazaar.placeholder.invalid/resource",
    "BAZAAR_PAY_TO": "0x000000000000000000000000000000000000dEaD",
    "BAZAAR_PRICE_MINOR": "10000",
    "BAZAAR_CONTENT_TYPE": "application/json",
    "BAZAAR_TOOL_NAME": "get_public_data",
}

COMMAND = ["uv", "run", "--directory", "<repo>", "python", "-m", "payguard.server"]

# Names only, never values. The operator keeps the values in <repo>/.env (see .env.example).
ENV_REQUIRED = {
    "BUYER_KEY_SEPOLIA": "Buyer wallet private key on Base Sepolia (secret; scripts/setup_wallets.py generates it).",
    "SELLER_BASE_URL": "Base URL of the demo x402 sellers.",
    "RPC_URL_SEPOLIA": "Base Sepolia JSON-RPC URL used to prove settlement on-chain.",
}
ENV_OPTIONAL = {
    "BUYER_KEY_MAINNET": "Buyer wallet private key on Base mainnet (secret; real funds; only for the Bazaar tool).",
    "RPC_URL_MAINNET": "Base mainnet JSON-RPC URL.",
    "PAYGUARD_STORE": "sqlite (default) or supabase.",
    "PAYGUARD_SQLITE_PATH": "SQLite purchase ledger path.",
    "PAYGUARD_ARTIFACT_DIR": "Where delivered artifacts are stored.",
    "PAYGUARD_POLICY": "Spending policy YAML path.",
    "PAYGUARD_LOG_LEVEL": "Log level (logs go to stderr).",
    "SUPABASE_URL": "Supabase project URL when PAYGUARD_STORE=supabase.",
    "SUPABASE_SERVICE_KEY": "Supabase service key (secret) when PAYGUARD_STORE=supabase.",
    "INTEL_BASE_URL": "Enables get_intel_report (Meridian Intel demo seller).",
    "PRIMITIVE_API_KEY": "Primitive mail key (secret) for file_dispute/check_dispute and auto_dispute; empty = offline dry run.",
    "PRIMITIVE_FROM": "The buyer agent's own Primitive inbox address (required when PRIMITIVE_API_KEY is set).",
    "SELLER_CONTACT_EMAIL": "Fallback dispute recipient for sellers without /.well-known/x402-seller.json.",
    "BAZAAR_URL": "Enables the Bazaar tool (public x402 seller on Base mainnet).",
    "BAZAAR_PAY_TO": "Bazaar seller pay-to address.",
    "BAZAAR_PRICE_MINOR": "Bazaar accepted price in USDC minor units.",
    "BAZAAR_CONTENT_TYPE": "Bazaar expected content type.",
    "BAZAAR_TOOL_NAME": "Bazaar tool name (default get_public_data).",
}

PRIVATE_KEY = re.compile(r"(?<![0-9a-fA-F])(0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def find_key_like(obj: Any) -> list[str]:
    """Every string in obj containing 64 hex characters in a row (a private key or anything that looks like one)."""
    return [s for s in _strings(obj) if PRIVATE_KEY.search(s)]


async def build() -> dict[str, Any]:
    os.environ.update(PLACEHOLDER_ENV)  # before the import: tool registration reads these at import time
    from payguard.server import mcp

    tools = sorted(await mcp.list_tools(), key=lambda t: t.name)
    return {
        "server": "payguard",
        "description": "Buyer protection for x402 purchases: pays once, verifies delivery, proves settlement on-chain, "
                       "files disputes.",
        "transport": "stdio",
        "command": COMMAND,
        "env": {"required": ENV_REQUIRED, "optional": ENV_OPTIONAL},
        "env_gated_tools": {"get_intel_report": "INTEL_BASE_URL", "get_public_data": "BAZAAR_URL + BAZAAR_PRICE_MINOR"},
        "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools],
    }


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--check", action="store_true", help="compare with --out instead of writing it")
    args = ap.parse_args(argv)

    doc = asyncio.run(build())
    bad = find_key_like(doc)
    if bad:
        print(f"refusing to export: {len(bad)} string(s) look like a 64-hex private key", file=sys.stderr)
        return 2
    text = render(doc)
    if args.check:
        current = args.out.read_text() if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale; run: uv run python scripts/export_tool_schema.py", file=sys.stderr)
            return 1
        return 0
    args.out.write_text(text)
    print(f"wrote {args.out} ({len(doc['tools'])} tools)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
