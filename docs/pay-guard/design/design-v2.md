# BillGuard — Hackathon MVP Design (v2)

**Date:** 2026-10-17
**Event:** YC "Make Something Agents Want", overnight hackathon
**Priority:** proof of concept and a convincing live demo, not production
**Source spec:** `~/Downloads/writing-block (1).md` (BillGuard product spec)
**Supersedes:** `2026-10-17-design.md`, revised per `design-review-1.md`

SDK citations are relative to the installed `x402/` package (2.25.0).

## 0. Changes from previous version

- **C1:** Every `x402Client` (BillGuard and `smoke_sellers.py`) disables the SDK's $1 default cap. A signing-time exception is recorded as `REJECTED`, never `SETTLEMENT_UNKNOWN`.
- **C2:** The facilitator client is built from `FacilitatorConfig`, never from a bare string.
- **C3:** After send, only 2xx plus a decoded `success=true` is a fast-path `SETTLED`. Every other outcome, including 2xx with no header, goes to `SETTLEMENT_UNKNOWN` and is reconciled immediately in the same call. `PAYMENT_FAILED` now requires chain proof.
- **C4:** Added a fingerprint-only duplicate guard (`DUPLICATE_SUSPECTED`), an `attempt` column in the unique key, and `supersedes_purchase_id` so that `force_new_payment` has a row to live in.
- **S1:** The signed requirement is pinned to the authorized one, then asserted after signing (amount, asset).
- **S2:** The full encoded `PAYMENT-SIGNATURE` header is persisted. An unknown purchase still inside its window is resent with the same header, not refused.
- **S3–S5:** `asyncio.Lock` plus DB unique constraint, `IN_PROGRESS` response, explicit budget set, startup recovery.
- **S6:** Scenario B uses a dedicated `/research-slow` route with an async after-settle hook. The proxy fallback is removed.
- **S7:** The stub facilitator fallback is replaced by a self-hosted SDK facilitator on Sepolia. The stub is used in pytest only.
- **S8, S14:** Bazaar discovery and mainnet funding move to hour 1, filtered to v2 sellers. Build order is cut. The backup video is recorded after step 5.
- **S9:** Supabase stays the store, behind a repository interface with a SQLite drop-in.
- **S10–S13:** Hand-written tools, PDF returned as a path, "naive retry pays twice" contrast, ~300 s deadline shown.

## 1. Decisions

| Decision | Choice | Why |
|---|---|---|
| Payment rail | Real x402 v2 on Base Sepolia for own sellers, plus one public v2 Bazaar seller on Base mainnet | Strongest proof that sellers need no BillGuard integration |
| Stack | Python, `x402` 2.25.0 | Verified: v2 header helpers, `x402Client`, FastAPI `payment_middleware`, `HTTPFacilitatorClient`, `EthAccountSigner`, hooks |
| Buying agent | Claude Desktop over stdio | Zero agent code; satisfies the "external agent client" criterion |
| Purchase flow | Explicit state machine, persisted before every network step | Signed payload is on disk before send, so a timeout can be reconciled |
| Durable store | **Supabase Postgres** via `supabase-py`, behind a `Repository` protocol. **SQLite is a drop-in fallback** (`BILLGUARD_STORE=sqlite`) if Supabase or venue Wi-Fi misbehaves on the night | Sponsor fit and a table view, with an escape hatch |
| Facilitator | `https://x402.org/facilitator`; fallback is a **self-hosted SDK facilitator** on Sepolia | A fallback must still put transactions on-chain, or reconciliation has nothing to find |

Sponsor fit: **Supabase** is the store. Armature, Stripe, Manufact, Blaxel and Inkbox are cut (S14).

The full "SQLite as truth plus a Supabase mirror" design is rejected. Two write paths cost more than a switchable repository.

## 2. Architecture

One `uv` monorepo. Three processes: the MCP server, the demo sellers, and Claude Desktop.

```
pay-guard/
  billguard/
    server.py        # FastMCP stdio; 4 hand-written paid tools + admin tools
    registry.py      # allowlisted services (dataclasses)
    policy.py        # mandate from policy.yaml (read-only to agent)
    purchase.py      # state machine (section 3)
    reconcile.py     # EIP-3009 authorizationState check
    verify.py        # hard-coded validators
    store/
      base.py        # Repository protocol
      supabase.py    # primary
      sqlite.py      # drop-in fallback, same schema
    wallet.py        # EthAccountSigner from env, per network
  sellers/
    app.py           # FastAPI + official payment_middleware; ZERO billguard imports
    facilitator.py   # fallback self-hosted SDK facilitator (not started by default)
  scripts/
    setup_wallets.py # keys, faucet links, USDC balances (both networks)
    smoke_sellers.py # pay each seller with x402HttpxClient; --naive-retry flag
    bazaar_probe.py  # list Bazaar sellers, keep v2 only, smoke two
    reset_demo.py
  tests/
  policy.yaml
```

The `Repository` protocol covers purchase create/get/update, lookup by key and by open fingerprint, evidence, findings, `spent_minor` and `list_stuck`. `supabase-py` is synchronous; blocking calls in async handlers are acceptable for the demo.

**Wallets.** `BUYER_KEY_SEPOLIA` and `BUYER_KEY_MAINNET` (a few dollars of USDC) come from env. Each registry entry pins one CAIP-2 network, so the mainnet key never pays a Sepolia service. "Secrets never in evidence" means private keys. Signed authorizations are bearer tokens bound to one payTo, amount and time window, and they are stored deliberately (S2).

**Registry fields:** `id`, `tool_name`, `method`, `url_template`, `network`, `asset` (canonical USDC from `mechanisms/evm/default_assets.py:30-46`), `accepted_price_minor`, `expected_content_type`, `validators`.

**Policy fields:** `principal_id`, `total_budget_minor`, `per_purchase_cap_minor`, `allowed_networks`, `allowed_services`.

**SDK client construction** (C1, `client_base.py:184,601-605,670-676`):

```python
client = x402Client()
client.register("eip155:*", ExactEvmScheme(EthAccountSigner(account)))
client.set_spend_controls({"max_amount_per_payment": False})  # BillGuard owns the mandate
```

## 3. Purchase state machine

**Key.** `request_fingerprint` = sha256(service_id + canonical JSON of args). The unique constraint is `(principal_id, purchase_intent_id, request_fingerprint, attempt)`, with `attempt` starting at 1. `supersedes_purchase_id` links a forced retry to the row it replaces.

```
INTENT_RECORDED -> QUOTED -> AUTHORIZED | REJECTED
AUTHORIZED -> PAYLOAD_SIGNED | REJECTED (signing exception)
PAYLOAD_SIGNED -> SENT -> SETTLED | SETTLEMENT_UNKNOWN
SETTLEMENT_UNKNOWN -> SETTLED | PAYMENT_FAILED   (only via reconcile)
```

Fulfillment is a separate column: `PENDING | VERIFIED | FAILED | UNVERIFIED | NO_DELIVERY`.

**Budget (S4).** Spent is the sum of `authorized_amount_minor` over `{AUTHORIZED, PAYLOAD_SIGNED, SENT, SETTLEMENT_UNKNOWN, SETTLED}`. `REJECTED` and `PAYMENT_FAILED` are excluded.

**Concurrency (S3).** One process-wide `asyncio.Lock` covers the lookup, the duplicate guard, row creation and authorization. The DB unique constraint is the backstop. With supabase-py, `upsert(..., ignore_duplicates=True)` returns empty data on conflict, so it is followed by a select. A call that finds a row in `INTENT_RECORDED..SENT` returns `IN_PROGRESS` and neither pays nor reconciles.

**Steps.** Each step writes to the store before the next network call.

1. **Intent.**
   - If a row exists for `(principal, intent, fingerprint)`, return the latest attempt. If it is `SETTLEMENT_UNKNOWN`, reconcile first.
   - **Duplicate guard (C4).** Otherwise look up `(principal, fingerprint)` from the last 15 min. If a row exists in `INTENT_RECORDED..SENT` or `SETTLEMENT_UNKNOWN`, or is `SETTLED` with fulfillment not `VERIFIED`, return that row with a `DUPLICATE_SUSPECTED` finding and `safe_next_actions: ["reconcile_purchase", "force_new_payment=true"]`.
   - The guard does not depend on Claude reusing an intent ID, so scenario B is deterministic.
   - Store unreachable → fail here, before any signing.
2. **Quote.**
   - Send an unpaid request. Run `detect_payment_required_version` (`http/utils.py:80-114`), and if the version is not 2, reject with `UNSUPPORTED_X402_VERSION`.
   - Decode with `decode_payment_required_header` (`http/utils.py:54-65`). Store the full requirements as evidence `quote`.
   - Select the `exact` option whose `network` and `asset` match the registry. Store it as `selected_requirement`.
3. **Authorize.**
   - `PRICE_MISMATCH` if `amount > accepted_price_minor` or the asset is not canonical USDC.
   - `BUDGET_EXCEEDED` if the amount is over the per-purchase cap or the remaining budget.
   - Store evidence `authorization`.
4. **Sign (S1).**
   - Call `create_payment_payload(payment_required.model_copy(update={"accepts": [selected]}))`. Without this, the SDK re-selects (`client_base.py:773`) and its default selector picks `accepts[0]` (`client_base.py:322-327`).
   - Assert `payload.accepted == selected` and `payload.payload["authorization"]["value"] == authorized_amount`.
   - Encode with `encode_payment_signature_header` (`http/utils.py:28-44`). Store **the full encoded header** plus `from`, `to`, `value`, `nonce`, `validBefore` as evidence `payment_payload`. State becomes `PAYLOAD_SIGNED`.
   - Any exception in this step → `REJECTED` with reason `SIGNING_FAILED`. No money is at risk.
5. **Send.** Retry with the `PAYMENT-SIGNATURE` header and a 15 s timeout. State becomes `SENT`.
6. **Outcome (C3).**
   - **2xx and `decode_payment_response_header` (`http/utils.py:74-77`) gives `success=true`** → `SETTLED`. Store evidence `settlement` (transaction, network, payer, amount) and the body as an artifact.
   - **Everything else** → `SETTLEMENT_UNKNOWN`, then reconcile immediately in the same tool call. "Everything else" includes:
     - a 402 with `success=false` (`fastapi.py:441-467`, `unexpected_settle_error`)
     - a 502 (`fastapi.py:439-440`)
     - any other 4xx/5xx
     - a timeout or connection error
     - a decode error
     - a 2xx with no `PAYMENT-RESPONSE`
   - In the 2xx-without-header case, store the body as an artifact *first*, then reconcile, then verify.
7. **Verify.** Run the validators (section 5) when an artifact exists. If the row is `SETTLED` with no artifact → fulfillment `NO_DELIVERY` and a `PAID_NO_DELIVERY` finding.

**`force_new_payment=true` (C4).** This creates a new row with `attempt = max + 1` and `supersedes_purchase_id` set. It is allowed only if the prior row is `SETTLED` (not verified) or `PAYMENT_FAILED`. It is refused while the prior row is in flight or `SETTLEMENT_UNKNOWN`, and the response shows the deadline. The new payment counts against the budget.

**Startup recovery (S5).** On server start, inside the lock:
- `INTENT_RECORDED`, `QUOTED` or `AUTHORIZED` → `PAYMENT_FAILED`. Nothing was signed, so the budget is freed.
- `PAYLOAD_SIGNED` or `SENT` → `SETTLEMENT_UNKNOWN`, then reconcile.

Claude Desktop restarts stdio servers freely, so this path will run during rehearsal.

**Data model.**
- `purchases`: purchase_id, principal_id, task_id, purchase_intent_id, request_fingerprint, attempt, supersedes_purchase_id, service_id, quoted_amount_minor, authorized_amount_minor, asset, network, pay_to, payment_status, fulfillment_status, settlement_tx, valid_before, artifact_hash, created_at, updated_at. Unique: (principal_id, purchase_intent_id, request_fingerprint, attempt).
- `evidence`: evidence_id, purchase_id, kind (`quote`, `selected_requirement`, `authorization`, `payment_payload`, `settlement`, `seller_error`, `chain_check`, `artifact`, `validator`), payload jsonb, created_at.
- `findings`: finding_id, purchase_id, code, severity (`proven` | `suspected`), summary, evidence_ids, safe_next_action, resolved_at.

Finding codes: `PRICE_MISMATCH`, `BUDGET_EXCEEDED`, `SETTLEMENT_UNKNOWN`, `PAYMENT_FAILED`, `PAID_NO_DELIVERY`, `FULFILLMENT_FAILED`, `FULFILLMENT_UNVERIFIED`, `DUPLICATE_SUSPECTED`.

Money is always integer minor units (USDC, 6 decimals).

## 4. Demo sellers (official middleware, no BillGuard code)

One FastAPI app with `payment_middleware`. The facilitator client is built as `HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))` (C2, `http/facilitator_client_base.py:155-176`), where `FACILITATOR_URL` defaults to `DEFAULT_FACILITATOR_URL` (`http/constants.py:16`). Network is `eip155:84532`.

The middleware calls `/supported` at startup and exits on a capability mismatch. Starting it in hour 1 therefore confirms that x402.org supports Sepolia.

| Route | Price | Behavior | Staging |
|---|---|---|---|
| `GET /report/{ticker}` | $0.50 | JSON company record | none |
| `GET /research-slow?topic=` | $0.75 | JSON research result | `on_after_settle` (`server.py:141-144`) runs `async def` with `await asyncio.sleep(90)` when `ctx.transport_context.request.url.path == "/research-slow"`. Settlement lands on-chain and the client times out at 15 s. Never use `time.sleep`, which would block the event loop for every route. |
| `POST /convert` `{doc_id, pages}` | $2.00 | PDF built with pypdf | always returns 12 pages |
| `GET /report-premium/{ticker}` | $0.75 | same as report | registry accepts $0.50 → `PRICE_MISMATCH` before signing |

`smoke_sellers.py` uses `x402HttpxClient` with the same `set_spend_controls({"max_amount_per_payment": False})`, so `/convert` passes. `--naive-retry` hits `/research-slow` twice with the plain SDK client and shows two on-chain transfers (S12).

**Public seller.** In hour 1, `bazaar_probe.py` lists CDP Bazaar resources, keeps cheap v2 sellers on `eip155:8453` with USDC, and smoke-tests two of them with `x402HttpxClient`. The winner is registered with the `non_empty` and `content_hash` validators. Supporting v1 sellers is rejected because a second header and body format costs time when v2 candidates exist.

**Fallback facilitator (S7).** `sellers/facilitator.py` is a self-hosted `x402Facilitator` (`facilitator.py:43`) with `FacilitatorWeb3Signer` (`mechanisms/evm/signers.py:264`) and the exact EVM facilitator scheme (`mechanisms/evm/exact/facilitator.py:88`), using a Sepolia-ETH-funded key. Switching to it means changing `FACILITATOR_URL`. Transactions stay on-chain, so reconciliation still works. The pure stub exists in pytest only.

## 5. Reconciliation and verification

**Reconciliation** (`reconcile.py`) applies to `SETTLEMENT_UNKNOWN` rows. It is triggered right after an ambiguous outcome, on a repeated intent or duplicate hit, by `reconcile_purchase`, and at startup.

1. Read `from`, `nonce`, `validBefore` and the encoded header from `payment_payload` evidence.
2. Call `authorizationState(from, nonce)` via web3.py using `AUTHORIZATION_STATE_ABI` (`mechanisms/evm/constants.py:432-444`).
   - Sepolia: RPC `https://sepolia.base.org`, USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e`.
   - Mainnet: RPC `https://mainnet.base.org`, USDC `0x8335...2913`.
3. Interpret the result:
   - **`true`** → `SETTLED`. Store evidence `chain_check` with the block number. With no artifact → `PAID_NO_DELIVERY`.
   - **`false` and `now > validBefore`** → `PAYMENT_FAILED`. This is proven, so paying again is safe.
   - **`false` and still valid** → **resend the stored identical header** (S2). This is idempotent because the nonce is single-use, and it runs the outcome rules again. If the resend is also ambiguous, the row stays `SETTLEMENT_UNKNOWN` and the response shows the deadline. `validBefore` is now plus `maxTimeoutSeconds`, about 300 s (`mechanisms/evm/exact/client.py:100`, `server_base.py:715`).

Verified: the exact scheme uses EIP-3009, not Permit2, for USDC on both Base networks (`mechanisms/evm/exact/client.py:86-115`, `default_assets.py:30-46`). The Permit2 contingency is dropped. Tx-hash lookup via Transfer logs is cut.

**Validators** (`verify.py`) are hard-coded functions with no DSL (S14). Each returns `(name, passed, detail)`:
- `json_schema(schema)`
- `ticker_matches` (checks `args["ticker"]`)
- `pdf_page_count` (checks `args["pages"]`)
- `content_type(expected)`
- `non_empty`
- `content_hash`

Any validator exception → `FULFILLMENT_UNVERIFIED`. Each failure creates a `FULFILLMENT_FAILED` finding that points at the validator evidence and the artifact evidence.

## 6. MCP tool surface (FastMCP, stdio)

**Paid tools** (S10) are four hand-written typed functions that each call a shared `purchase(service_id, args, purchase_intent_id, task_id=None, force_new_payment=False)`:
- `get_company_report`
- `get_research_report`
- `convert_document`
- `<public_seller_tool>`

**Response:**
- `purchase_id`, `attempt`
- `payment_status` (plus `IN_PROGRESS`)
- `settled_amount`, `currency`, `network`, `settlement_tx`
- `valid_before`, when unknown
- `fulfillment_status`, `verification`, `findings`
- `artifact`: inline JSON, or for PDFs a **local path plus page count and hash, never base64** (S11)
- `safe_next_actions`

**Admin tools:** `list_services`, `get_spending_policy`, `get_purchase`, `list_findings(unresolved_only=true)`, `reconcile_purchase`.

Tool descriptions ask the agent to reuse `purchase_intent_id` and check `findings` before spending; the duplicate guard enforces this regardless.

## 7. Testing

- **Pytest:** sellers run in-process with a stub facilitator. The state machine runs against the SQLite repository, which also exercises the fallback. Tests cover one case per transition, each outcome branch in step 6, the duplicate guard, `force_new_payment`, startup recovery, and each validator.
- **Manual:** one Sepolia run per scenario, then a cold-start Claude session with the spec prompt.

## 8. Overnight build order (checkpoint each)

1. **Hour 1, in parallel:**
   - **Track A:** wallets, Sepolia faucet USDC, seller app up (proves `/supported`), `smoke_sellers.py` pays `/report` and `/convert`.
   - **Track B:** fund the mainnet wallet with USDC, then run `bazaar_probe.py` and pick two v2 candidates.
   - **Checkpoint:** one real Sepolia settlement. Not done by 2 h → start the self-hosted facilitator.
2. **Core purchase flow.** Repository (Supabase plus SQLite), state machine, `/report` through BillGuard in Claude Desktop.
   - **Checkpoint:** Claude buys a report.
3. **Mandate and idempotency.** Mandate, duplicate guard, lock, startup recovery, `/report-premium`.
   - **Checkpoint:** a `PRICE_MISMATCH` rejection, and a fresh intent ID gets `DUPLICATE_SUSPECTED`.
4. **Conversion.** `/convert`, PDF validator, findings.
   - **Checkpoint:** `FULFILLMENT_FAILED` with evidence.
5. **Reconciliation.** `/research-slow` and reconciliation.
   - **Checkpoint:** `SETTLEMENT_UNKNOWN` → chain → `SETTLED` + `PAID_NO_DELIVERY`, and the retry is refused.
   - **Record the backup video now.**
6. **Bazaar mainnet purchase,** timeboxed to 1 h. Written fallback: show the discovery listing and the probe output, and say "the payment code path is identical."
7. **Stretch:** Supabase table view as a live ledger, `--naive-retry` contrast.

**Cut:** Armature, Stripe, Transfer-log lookup, ledger UI work, validator DSL.

Steps 3–5 are independent once step 2 lands.

## 9. Demo script (6 minutes)

1. Hook: "Agents can already spend money. Who checks they got what they paid for?"
2. `smoke_sellers.py`: these are ordinary x402 endpoints, and `sellers/app.py` has no BillGuard import. Optionally run `--naive-retry` to show the bare SDK paying twice.
3. Claude Desktop, prompt from spec §8, budget $5.
4. Report purchase → `VERIFIED`, with a Sepolia explorer link.
5. Research purchase → timeout → `SETTLEMENT_UNKNOWN` → reconciled on-chain within the same call → `SETTLED`, `PAID_NO_DELIVERY`. Claude retries, even with a new intent ID → `DUPLICATE_SUSPECTED`, with no second payment.
6. Convert purchase → 12 of 20 pages → `FULFILLMENT_FAILED` with page-count evidence.
7. `list_findings` → Claude summarizes what was spent, what was delivered, and what is unresolved.
8. Bazaar seller on mainnet: "this seller has never heard of us."
9. Limits: no escrow, no refunds, schema-valid is not fact-checked.

## 10. Open risks

- **Facilitator or RPC flakiness:** switch to the self-hosted facilitator, and play the backup video recorded after step 5.
- **Supabase or Wi-Fi trouble:** `BILLGUARD_STORE=sqlite`.
- **No usable v2 Bazaar seller, or mainnet funding delayed:** use the written fallback from step 6.
- **x402.org not supporting Sepolia:** detected at seller start in hour 1.
