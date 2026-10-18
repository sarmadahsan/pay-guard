# PayGuard

**Buyer protection for autonomous agents.** Agents can pay. PayGuard helps them verify what they paid for.

PayGuard is an MCP server that acts as a buyer-side purchasing gateway for x402 paid services. An agent calls an ordinary-looking tool; PayGuard records the offered price, enforces a spending mandate, pays through the standard x402 flow, captures settlement evidence, reconciles ambiguous outcomes on-chain, and runs deterministic checks on what was delivered. Sellers need no PayGuard-specific integration.

Built for the YC "Make Something Agents Want" hackathon, October 17–18, 2026.

## Status

Design and PRD complete, reviewed, and approved. Implementation starts next.

## Documents

- [Design (final, approved)](docs/pay-guard/design/design-final.md)
- [PRD](docs/pay-guard/prd/prd.md)
- Review history: [round 1](docs/pay-guard/design/design-review-1.md), [round 2](docs/pay-guard/design/design-review-2.md), [round 3](docs/pay-guard/design/design-review-3.md)
- Earlier drafts: [v1](docs/pay-guard/design/2026-10-17-design.md), [v2](docs/pay-guard/design/design-v2.md)

## Planned stack

Python, `x402` SDK 2.25.0 (v2 protocol), FastMCP over stdio, FastAPI demo sellers using the official x402 middleware, SQLite with a Supabase option, Base Sepolia for demo sellers and Base mainnet for one public seller.

## What PayGuard does not do

No escrow, no refunds, no fact-checking of purchased content, no universal MCP proxying. See the design's limits section.

## Run (hackathon)

    uv sync --group dev
    cp .env.example .env            # then fill keys via scripts/setup_wallets.py
    uv run uvicorn sellers.app:app --port 8402      # demo sellers
    uv run python -m payguard.server               # MCP server over stdio (Claude Desktop launches this)
    uv run pytest -q

Demo switches on the seller (env, default off): `SELLER_STALL_S` (after-settle stall on `/research-slow`) and
`SELLER_TRUNCATE_PATHS=/convert` (settled body cut to 60%; PayGuard reports a proven "pdf truncated" failure, see
docs/pay-guard/demo/truncation.md).

## Install from Manufact

PayGuard is not listed on Manufact. Manufact Cloud only hosts MCP servers reached over HTTPS, and PayGuard runs locally over stdio so wallet keys never leave your machine. Details and options: docs/pay-guard/demo/manufact.md.

Install it in Claude Desktop instead, with the same command Claude Desktop already uses:

1. Clone this repo, run `uv sync`, then `cp .env.example .env` and fill the keys with `uv run python scripts/setup_wallets.py`.
2. Add this to `claude_desktop_config.json` and restart Claude Desktop:

       {"mcpServers": {"payguard": {"command": "uv",
         "args": ["run", "--directory", "/absolute/path/to/pay-guard", "python", "-m", "payguard.server"]}}}

3. Ask Claude to run `list_services`.

You supply these values in your local `.env`, never in the client config: `BUYER_KEY_SEPOLIA`, `SELLER_BASE_URL` and `RPC_URL_SEPOLIA` are required. The optional ones (mainnet key and RPC, store and policy paths, `INTEL_BASE_URL`, `BAZAAR_*`) are listed in `mcp-tools.json`, which also holds the full tool surface. Regenerate it after changing a tool with `uv run python scripts/export_tool_schema.py`.

Plan: docs/pay-guard/design/2026-10-17-pay-guard-implementation-plan.md
