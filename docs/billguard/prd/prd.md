# BillGuard PRD — Hackathon MVP

**Date:** 2026-10-17
**Event:** YC "Make Something Agents Want", overnight hackathon, Oct 17–18 2026
**Design:** `../design/design-final.md` (reviewed twice; this PRD follows it)
**Status:** design approved by review round 3 (`../design/design-review-3.md`); ready for implementation planning

## 1. Problem

AI agents can now pay for APIs and tools autonomously over x402. Payment infrastructure answers "was the agent allowed to pay, and did it pay?" It does not answer "was the agent charged the price it accepted?" or "did it receive what it paid for?" A seller's successful-payment response is treated as proof of both, and a network timeout after settlement leaves an agent not knowing whether it has already paid. Agents then retry and pay twice, accept short deliveries, and keep no durable record tying intent, price, payment, and artifact together.

## 2. Goals

1. An agent buys from ordinary x402 v2 sellers through one MCP tool call and gets back the artifact plus an evidence-backed purchase verdict.
2. No seller-side integration: the demo sellers use only the official x402 middleware and import nothing from BillGuard. One public Bazaar seller on Base mainnet proves this against a stranger.
3. A spending mandate enforced outside the model: price mismatch and budget overrun are rejected before anything is signed.
4. Ambiguous settlement is reconciled on-chain, not guessed, and the agent cannot pay twice for the same thing without a human.
5. Objectively incomplete delivery (12 of 20 PDF pages) is detected and recorded with evidence.
6. Purchase history and findings survive the agent session and are readable from a fresh session.

## 3. Non-goals

- Escrow, refunds, chargebacks, or disputes with the seller.
- Semantic or factual verification of purchased content. Schema-valid is not fact-checked.
- Universal MCP proxying or dynamic tool discovery. The registry is a hand-written allowlist of four services.
- x402 v1 sellers, non-USDC assets, payment rails other than x402.
- Production hardening: multi-process locking, auth between agent and gateway, key management beyond env vars.
- Agent-initiated forced re-purchase. Only a human can approve a second payment for a superseded purchase.

## 4. Scale and priority

- **Scale:** one principal, one buyer wallet per network, four services, tens of purchases per rehearsal.
- **#1 priority:** proof of concept with a reliable 6-minute live demo. Demo reliability beats feature breadth, which is why SQLite is the build-night store and Supabase is a flip after the core scenarios pass.

## 5. Users and stories

**Primary user:** an autonomous agent (Claude Desktop over stdio in the demo) given a research task and a $5 budget.
**Secondary user:** the operator who configures the mandate, runs rehearsals, and is the only party who can approve a second payment.

- As an agent, I call `get_company_report(ticker, purchase_intent_id)` and receive the report plus `payment_status: SETTLED`, `fulfillment_status: VERIFIED`, and a settlement transaction, so I can cite what I paid and what I got.
- As an agent, when a purchase times out I get `SETTLEMENT_UNKNOWN` followed by an on-chain reconciliation in the same call, with a `history` that shows how the verdict was reached.
- As an agent, when I try the same purchase again I get the existing record and a `DUPLICATE_SUSPECTED` finding instead of a second charge, even if I used a new intent ID.
- As an agent, when a seller charges more than the registry's accepted price, the purchase is rejected with `PRICE_MISMATCH` before money moves.
- As an agent, when a converter returns 12 pages against a 20-page contract, I get `FULFILLMENT_FAILED` with the page-count evidence and `safe_next_actions` that never say "pay again".
- As an agent, I call `list_findings` and get every unresolved discrepancy with the evidence it rests on, so I can report spent, delivered, and unresolved.
- As an operator, I set the budget and caps in `policy.yaml`, reset the ledger between rehearsals with `reset_demo.py --yes`, and approve a re-purchase only via `approve_retry.py`.

## 6. Requirements

### Functional

| ID | Requirement |
|---|---|
| F1 | FastMCP stdio server exposing four paid tools (`get_company_report`, `get_research_report`, `convert_document`, one Bazaar seller) and admin tools `list_services`, `get_spending_policy`, `get_purchase`, `list_findings`, `reconcile_purchase`. |
| F2 | Paid tools take `purchase_intent_id` and `task_id`; none exposes a force-retry parameter, and no MCP tool can approve a second payment. The only path is the operator-run `scripts/approve_retry.py`. |
| F3 | Purchase flow is the explicit state machine in design §3, with every transition persisted by compare-and-set before the next network call, and the signed `PAYMENT-SIGNATURE` header stored before send. |
| F4 | Quote step rejects non-x402 routes (`NOT_X402`), non-v2 responses, non-canonical USDC, price above `accepted_price_minor` (`PRICE_MISMATCH`), and amounts over cap or remaining budget (`BUDGET_EXCEEDED`), all before signing. |
| F5 | Only a 2xx with a decoded `PAYMENT-RESPONSE` of `success=true` is `SETTLED`. Every other post-send outcome is `SETTLEMENT_UNKNOWN` and reconciled in the same call. |
| F6 | Reconciliation polls EIP-3009 `authorizationState(from, nonce)` on the USDC contract (up to 5 × 2 s), resends the identical stored header at most once per call while the authorization is still valid, decides `PAYMENT_FAILED` by block timestamp past `validBefore`, and never creates a new payment. Each tool call has a 45 s wall-clock budget. |
| F7 | Duplicate guard: a repeat of an in-flight, unknown, or settled-but-unverified purchase returns the existing row with `DUPLICATE_SUSPECTED`, whether the agent reuses the intent ID or supplies a new one with the same `(principal, request_fingerprint)` within 15 minutes. A service with an unresolved `PAID_NO_DELIVERY` finding rejects all new purchases with `SERVICE_QUARANTINED`. |
| F8 | Validators: `json_schema`, `ticker_matches`, `pdf_page_count`, `content_type`, `non_empty`, `content_hash`. A failing validator creates `FULFILLMENT_FAILED` referencing evidence rows; a raising validator yields `FULFILLMENT_UNVERIFIED`. Settled with no artifact yields `PAID_NO_DELIVERY`. |
| F9 | Budget spent = sum of `authorized_amount_minor` over `AUTHORIZED, PAYLOAD_SIGNED, SENT, SETTLEMENT_UNKNOWN, SETTLED`. Amounts are integer minor units. |
| F10 | Startup recovery runs as a background task after the MCP initialize handshake, touches only rows older than 60 s using compare-and-set writes, moves unsigned rows to `ABANDONED` and signed rows to `SETTLEMENT_UNKNOWN`, then reconciles. |
| F11 | Demo sellers: one FastAPI app with the official `payment_middleware` on `eip155:84532`; routes `/report`, `/research-slow` (after-settle async sleep gated on that path only), `/convert` (always 12 pages), `/report-premium`. Zero BillGuard imports. |
| F12 | Scripts: `setup_wallets.py`, `smoke_sellers.py` (with `--naive-retry`), `bazaar_probe.py`, `approve_retry.py`, `reset_demo.py --yes`. |
| F13 | Store behind a `Repository` protocol with SQLite (default) and Supabase backends selected by `BILLGUARD_STORE`. |

### Non-functional

- Buyer HTTP timeout is per service: 15 s for `/research-slow`, 40 s otherwise. The stall hook sleeps 90 s; `validBefore` is about 300 s.
- All logging goes to stderr; stdout is reserved for MCP JSON-RPC. SQLite runs in WAL mode with a 5 s busy timeout.
- Private keys only from env and never written to evidence. Signed authorizations are stored deliberately.
- Facilitator defaults to `https://x402.org/facilitator`, with an in-process SDK facilitator fallback (`SELLER_FACILITATOR=local`) that still settles on-chain.
- Every finding references at least one evidence row. Findings carry `severity: proven | suspected`.

## 7. Success metrics (demo acceptance)

1. `smoke_sellers.py` pays `/report` and `/convert` with the plain SDK client, and `sellers/app.py` has no `billguard` import.
2. From Claude Desktop, one real Sepolia settlement returns `SETTLED` + `VERIFIED` with a transaction hash.
3. `/report-premium` yields `PRICE_MISMATCH` with no signed payload.
4. `/research-slow` yields `SETTLEMENT_UNKNOWN` then `SETTLED` + `PAID_NO_DELIVERY` within one tool call; the presenter's "try again" yields `DUPLICATE_SUSPECTED` (same intent ID or new one) or `SERVICE_QUARANTINED` (reworded topic), and exactly one on-chain transfer.
5. `/convert` yields `FULFILLMENT_FAILED` citing page count 12 vs 20.
6. `list_findings` from a fresh Claude session returns the above findings.
7. One Bazaar purchase on Base mainnet settles through the same code path, or the written fallback is shown.
8. The paid-tool schemas contain no force-retry parameter and no tool named `approve_retry` exists in the MCP schema.

## 8. Open questions

1. Which Bazaar seller: pick two v2 USDC candidates in hour 1 and keep the cheaper working one.
2. Does the x402.org facilitator stay responsive overnight? The local facilitator and the backup video are the hedges.
3. Keep Supabase from the start or flip after step 5? Default is flip; the operator can set `BILLGUARD_STORE=supabase` at any point.
4. How large a PDF artifact to return as a path vs inline? Paths for all PDFs is the current answer.
5. ~~Is 15 minutes the right duplicate-guard window?~~ Closed by review round 3: yes, given `reset_demo.py` runs between rehearsals.
