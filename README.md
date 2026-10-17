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
