# BillGuard — Hackathon MVP Design (final)

**Date:** 2026-10-17
**Event:** YC "Make Something Agents Want", overnight hackathon
**Priority:** proof of concept and a convincing live demo, not production
**Source spec:** `~/Downloads/writing-block (1).md` (BillGuard product spec)
**Supersedes:** `design-v2.md`, revised per `design-review-2.md`. **Approved** by `design-review-3.md`; its build-time suggestions S1, S2, S3, S8 and S9 are folded in below.

SDK citations are relative to the installed `x402/` package (2.25.0). `fastapi.py` means `http/middleware/fastapi.py`.

## 0. Changes from previous version

**After round 3 (approved):**
- **S1:** A retry with the *same* intent ID on a row that is in flight, `SETTLEMENT_UNKNOWN`, or settled-but-not-verified now attaches the `DUPLICATE_SUSPECTED` finding once and appends a `retry_blocked` evidence row per hit, so the demo's "try again" beat works whether or not Claude reuses the ID.
- **S2:** Buyer HTTP timeout is a per-service registry field `timeout_s`: 15 s for `/research-slow`, 40 s for every other route and the Bazaar seller. A slow facilitator can no longer turn scenario A into scenario B.
- **S3:** A service with an unresolved `PAID_NO_DELIVERY` finding is quarantined. New purchases from it are rejected with `SERVICE_QUARANTINED` before quoting, so a reworded research topic cannot pay again.
- **S8:** The `approve_retry` MCP admin tool is removed entirely. A second payment for a superseded purchase is possible only through `scripts/approve_retry.py`. `BILLGUARD_ALLOW_FORCE` is gone.
- **S9:** `create_payment_payload` is awaited. The hook unit test passes a dummy adapter and method. Demo step 9 no longer names a stretch script.
- Also taken from round 3: a 45 s wall-clock budget per tool call (S4), `PAYMENT_FAILED` decided by block timestamp (S5), Bazaar probe rejects Permit2 sellers and sets `timeout=60` (S6), logging to stderr and SQLite WAL + `busy_timeout` (S7).

**After round 2:**

- **N1:** The scenario B stall hook gates on `ctx.transport_context is not None and ctx.transport_context.request.path == "/research-slow"`. `request` is an `HTTPRequestContext` (`fastapi.py:270-278,331`; fields `adapter`, `path`, `method`, `payment_header`, `route_pattern`, `decoded_path` at `http/types.py:80-99`), not a Starlette `Request`. It has no `.url`. A pytest asserts that `/report` and `/convert` still deliver with the hook installed.
- **N2:** `force_new_payment` is removed from every agent-facing tool schema. A second payment for a superseded purchase is only possible through a human path: `scripts/approve_retry.py <purchase_id>`, or the `approve_retry` admin tool, which is registered only when `BILLGUARD_ALLOW_FORCE=1`. The demo default is `0`. `safe_next_actions` never suggests paying again.
- **Sugg. 1, 2:** Reconcile polls the chain first (up to 5 × 2 s). It resends the identical header at most **once per tool call**, and the resend outcome never re-enters reconcile. A resend 402 only triggers one more chain re-poll.
- **Sugg. 3:** The fallback facilitator is now an in-process `x402Facilitator` passed into `x402ResourceServer`, about 20 lines behind `SELLER_FACILITATOR=local`. It needs a key funded with Base Sepolia ETH and is prepared in hour 1.
- **Sugg. 4:** `reset_demo.py` is specified. It wipes purchases, evidence and findings (which also clears the duplicate guard). The $5 budget is per reset.
- **Sugg. 5, 6, 8:** Startup recovery only touches rows older than 60 s, uses compare-and-set writes and a new `ABANDONED` state, and runs in the background after initialize. After a proven failure, the same key auto-creates `attempt+1`. The lock is released before network I/O.
- **Sugg. 7, 9, 11:** The response has a `history` array, and the presenter's "try again" line is scripted. Repeating a verified purchase pays again by design. A `ValueError` from version detection maps to `REJECTED/NOT_X402`.
- **Sugg. 10 / store:** SQLite is the build-night default (see section 1). Pytest is trimmed. The mainnet buyer needs USDC only.

## 1. Decisions

| Decision | Choice | Why |
|---|---|---|
| Payment rail | Real x402 v2 on Base Sepolia for own sellers, plus one public v2 Bazaar seller on Base mainnet | Strongest proof that sellers need no BillGuard integration |
| Stack | Python, `x402` 2.25.0 | Verified: v2 header helpers, `x402Client`, FastAPI `payment_middleware`, `HTTPFacilitatorClient`, `EthAccountSigner`, hooks |
| Buying agent | Claude Desktop over stdio | Zero agent code; satisfies the "external agent client" criterion |
| Purchase flow | Explicit state machine, persisted before every network step, compare-and-set writes | Signed payload is on disk before send, so a timeout can be reconciled |
| Durable store | `Repository` protocol with two backends. **SQLite is the default for the build night** (`BILLGUARD_STORE=sqlite`). **Flip to Supabase once steps 1-5 pass**, since the table view is the live-ledger stretch | No Wi-Fi dependency on the core path |
| Facilitator | `https://x402.org/facilitator` (verified live for `exact`/`eip155:84532` v2). The fallback is an **in-process SDK facilitator** (`SELLER_FACILITATOR=local`) | A fallback must still put transactions on-chain, or reconciliation has nothing to find |
| Retry authority | Only a human can approve a second payment for a superseded purchase, via `scripts/approve_retry.py` (N2, S8) | The spec requires spending authorization to be enforced outside the language model |

Starting with SQLite is the reviewers' recommendation. The user may keep Supabase from the start by setting `BILLGUARD_STORE=supabase`; nothing else changes.

Sponsor fit: **Supabase** is the ledger after the flip. Armature, Stripe, Manufact, Blaxel and Inkbox are cut.

## 2. Architecture

One `uv` monorepo. Three processes: the MCP server, the demo sellers, and Claude Desktop.

```
pay-guard/
  billguard/
    server.py        # FastMCP stdio; 4 paid tools + admin tools; logging to stderr
    registry.py      # allowlisted services (dataclasses)
    policy.py        # mandate from policy.yaml (read-only to agent)
    purchase.py      # state machine (section 3)
    reconcile.py     # EIP-3009 authorizationState check, one bounded resend
    verify.py        # hard-coded validators
    store/
      base.py        # Repository protocol
      sqlite.py      # default (build night); WAL mode, busy_timeout 5000 ms
      supabase.py    # flipped on after step 5
    wallet.py        # EthAccountSigner from env, per network
  sellers/
    app.py           # FastAPI + official payment_middleware; ZERO billguard imports
    local_facilitator.py  # in-process SDK facilitator, used iff SELLER_FACILITATOR=local
  scripts/
    setup_wallets.py # keys, faucet links, USDC balances (both networks), Sepolia ETH for facilitator key
    smoke_sellers.py # pay each seller with x402HttpxClient; --naive-retry flag
    bazaar_probe.py  # list Bazaar sellers, keep v2 only, smoke two
    approve_retry.py # human-only second payment for a superseded purchase (N2)
    reset_demo.py    # wipe demo state between rehearsals (section 8)
  tests/
  policy.yaml
```

The `Repository` protocol covers:
- purchase create, get, and compare-and-set update (`update_status(id, expected, new, **fields)` returns bool)
- lookup by key and by open fingerprint
- evidence, findings, `spent_minor` and `list_stuck(older_than_s)`
- `reset(principal_id)`

`supabase-py` is synchronous; blocking calls in async handlers are acceptable for the demo.

**Wallets.** `BUYER_KEY_SEPOLIA` and `BUYER_KEY_MAINNET` come from env. The mainnet key needs only a few dollars of USDC and no ETH, because the facilitator pays gas for EIP-3009.

Each registry entry pins one CAIP-2 network, so the mainnet key never pays a Sepolia service. `FACILITATOR_KEY_SEPOLIA` needs a little Base Sepolia ETH and is used only by the fallback.

"Secrets never in evidence" means private keys. Signed authorizations are bearer tokens bound to one payTo, amount and time window, and they are stored deliberately.

**Registry fields:** `id`, `tool_name`, `method`, `url_template`, `network`, `asset` (canonical USDC from `mechanisms/evm/default_assets.py:30-46`), `accepted_price_minor`, `expected_content_type`, `validators`, `timeout_s` (S2: 15 for `/research-slow`, 40 otherwise).

**Policy fields:** `principal_id`, `total_budget_minor`, `per_purchase_cap_minor`, `allowed_networks`, `allowed_services`.

**SDK client construction** (`client_base.py:184,462-469,601-605`):

```python
client = x402Client()
client.register("eip155:*", ExactEvmScheme(EthAccountSigner(account)))
client.set_spend_controls({"max_amount_per_payment": False})  # BillGuard owns the mandate
```

## 3. Purchase state machine

**Key.** `request_fingerprint` = sha256(service_id + canonical JSON of args). The unique constraint is `(principal_id, purchase_intent_id, request_fingerprint, attempt)`, with `attempt` starting at 1. `supersedes_purchase_id` links a new attempt to the row it replaces.

```
INTENT_RECORDED -> QUOTED -> AUTHORIZED | REJECTED
AUTHORIZED -> PAYLOAD_SIGNED | REJECTED (signing exception)
PAYLOAD_SIGNED -> SENT -> SETTLED | SETTLEMENT_UNKNOWN
SETTLEMENT_UNKNOWN -> SETTLED | PAYMENT_FAILED   (only via reconcile)
INTENT_RECORDED | QUOTED | AUTHORIZED -> ABANDONED   (startup recovery only; nothing signed)
```

Fulfillment is a separate column: `PENDING | VERIFIED | FAILED | UNVERIFIED | NO_DELIVERY`.

Every transition is a compare-and-set (`update ... where payment_status = <expected>`). A lost race aborts the call before any signing.

**Budget.** Spent is the sum of `authorized_amount_minor` over `{AUTHORIZED, PAYLOAD_SIGNED, SENT, SETTLEMENT_UNKNOWN, SETTLED}`. `REJECTED`, `PAYMENT_FAILED` and `ABANDONED` are excluded.

**Concurrency (Sugg. 8).** One process-wide `asyncio.Lock` covers the lookup, the duplicate guard, row creation and authorization. It is **released before any network I/O** (quote, send, chain poll, resend). The DB unique constraint and the compare-and-set writes are the backstop.

With supabase-py, `upsert(..., ignore_duplicates=True)` returns empty data on conflict, so it is followed by a select. A call that finds a row in `INTENT_RECORDED..SENT` returns `IN_PROGRESS` and neither pays nor reconciles.

**Steps.** Each step writes to the store before the next network call. Each transition is appended to the row's `history` (Sugg. 7).

1. **Intent.**
   - If a row exists for `(principal, intent, fingerprint)`, look at the latest attempt:
     - `SETTLEMENT_UNKNOWN` → reconcile first.
     - `PAYMENT_FAILED` or `ABANDONED` → create `attempt+1` with `supersedes_purchase_id` set (Sugg. 6). This is safe because no money moved.
     - In flight (`INTENT_RECORDED..SENT`) → return it with `IN_PROGRESS`.
     - `SETTLEMENT_UNKNOWN` (still, after reconcile) or `SETTLED` with fulfillment not `VERIFIED` → return it, attach the `DUPLICATE_SUSPECTED` finding if the row does not already have one, and append a `retry_blocked` evidence row (S1). **No payment.**
     - `SETTLED` + `VERIFIED` or `REJECTED` → return it unchanged.
   - **Service quarantine (S3).** Before anything else for a new key: if the service has an unresolved `PAID_NO_DELIVERY` finding for this principal, reject with `REJECTED/SERVICE_QUARANTINED` and `safe_next_actions: [human_review]`. No quote, no payment. `reset_demo.py` clears it.
   - **Duplicate guard.** Otherwise look up `(principal, fingerprint)` from the last 15 min.
     - It fires if a row is in `INTENT_RECORDED..SENT` or `SETTLEMENT_UNKNOWN`, or is `SETTLED` with fulfillment not `VERIFIED`.
     - It returns that row with a `DUPLICATE_SUSPECTED` finding. **No payment.**
     - The guard does not depend on Claude reusing an intent ID, so scenario B is deterministic.
     - An identical **verified** purchase under a fresh intent ID pays again by design (Sugg. 9).
   - Store unreachable → fail here, before any signing.
2. **Quote.**
   - Send an unpaid request, then run `detect_payment_required_version` (`http/utils.py:80-114`):
     - `ValueError` (unprotected route, no x402 headers) → `REJECTED/NOT_X402` (Sugg. 11).
     - Version is not 2 → `REJECTED/UNSUPPORTED_X402_VERSION`.
   - Decode with `decode_payment_required_header` (`http/utils.py:54-65`). Store the full requirements as evidence `quote`.
   - Select the `exact` option whose `network` and `asset` match the registry. Store it as `selected_requirement`.
3. **Authorize.**
   - `PRICE_MISMATCH` if `amount > accepted_price_minor` or the asset is not canonical USDC.
   - `BUDGET_EXCEEDED` if the amount is over the per-purchase cap or the remaining budget.
   - Store evidence `authorization`.
4. **Sign.**
   - `await client.create_payment_payload(payment_required.model_copy(update={"accepts": [selected]}))` (it is async, `client.py:173`). Without this, the SDK re-selects (`client_base.py:773`) and its default selector picks `accepts[0]` (`client_base.py:322-327`).
   - Assert `payload.accepted == selected` and `payload.payload["authorization"]["value"] == authorized_amount`.
   - Encode with `encode_payment_signature_header` (`http/utils.py:28-44`). Store the full encoded header plus `from`, `to`, `value`, `nonce`, `validBefore` as evidence `payment_payload`. State becomes `PAYLOAD_SIGNED`.
   - Any exception in this step → `REJECTED/SIGNING_FAILED`. No money is at risk.
5. **Send.** Retry with the `PAYMENT-SIGNATURE` header and the service's `timeout_s` (S2). State becomes `SENT`. The whole tool call has a 45 s wall-clock budget (S4); when it is exhausted the row stays `SETTLEMENT_UNKNOWN` and `safe_next_actions` includes `reconcile_purchase`.
6. **Outcome.**
   - **2xx and `decode_payment_response_header` (`http/utils.py:74-77`) gives `success=true`** → `SETTLED`. Store evidence `settlement` (transaction, network, payer, amount) and store the body as an artifact.
   - **Everything else** → `SETTLEMENT_UNKNOWN`, then reconcile immediately in the same tool call (section 5). "Everything else" includes:
     - a 402 with `success=false` (`fastapi.py:441-467`)
     - a 502 (`fastapi.py:439-440`)
     - any other 4xx/5xx
     - a timeout or connection error
     - a decode error
     - a 2xx with no `PAYMENT-RESPONSE`
   - In the 2xx-without-header case, store the body as an artifact first.
7. **Verify.** Run the validators (section 5) when an artifact exists. If the row is `SETTLED` with no artifact → fulfillment `NO_DELIVERY` and a `PAID_NO_DELIVERY` finding.

**Superseding a settled purchase (N2, S8).** The agent cannot do this; the paid tools take no such parameter and there is no MCP tool for it. The only path is `uv run scripts/approve_retry.py <purchase_id> [--reason ...]`, run by the operator. It creates `attempt = max + 1` with `supersedes_purchase_id` and `approved_by = cli`, resolves the service's `PAID_NO_DELIVERY` finding, then runs steps 2-7. The new payment counts against the budget. It is refused while the prior row is in flight or `SETTLEMENT_UNKNOWN`, and the refusal shows the deadline.

**`safe_next_actions` vocabulary.** The values are `get_purchase`, `reconcile_purchase` (only when `SETTLEMENT_UNKNOWN`), `wait_until:<valid_before>`, `human_review`, `report_to_seller` and `use_artifact`.
- No value means "pay" or "retry".
- `PAID_NO_DELIVERY`, `FULFILLMENT_FAILED` and `DUPLICATE_SUSPECTED` all return `["human_review", "report_to_seller"]`. `SERVICE_QUARANTINED` returns `["human_review"]`.

**Startup recovery (Sugg. 5).** FastMCP has no post-initialize callback, so the lifespan spawns recovery with `asyncio.create_task` after a 2 s sleep (S7); it never blocks the handshake. It only touches rows whose `updated_at` is older than 60 s, and every write is compare-and-set, so it cannot stomp on a second live process.
- `INTENT_RECORDED`, `QUOTED` or `AUTHORIZED` → `ABANDONED`. Nothing was signed, so the budget is freed and no "proven" finding is created.
- `PAYLOAD_SIGNED` or `SENT` → `SETTLEMENT_UNKNOWN`, then reconcile.

Claude Desktop restarts stdio servers freely, so this path will run during rehearsal.

**Data model.**
- `purchases`: purchase_id, principal_id, task_id, purchase_intent_id, request_fingerprint, attempt, supersedes_purchase_id, approved_by (null or `cli`), service_id, quoted_amount_minor, authorized_amount_minor, asset, network, pay_to, payment_status, fulfillment_status, settlement_tx, valid_before, artifact_hash, history jsonb, created_at, updated_at. Unique: (principal_id, purchase_intent_id, request_fingerprint, attempt).
- `evidence`: evidence_id, purchase_id, kind (`quote`, `selected_requirement`, `authorization`, `payment_payload`, `settlement`, `seller_error`, `chain_check`, `resend`, `retry_blocked`, `artifact`, `validator`), payload jsonb, created_at.
- `findings`: finding_id, purchase_id, code, severity (`proven` | `suspected`), summary, evidence_ids, safe_next_action, resolved_at.

Finding codes: `PRICE_MISMATCH`, `BUDGET_EXCEEDED`, `NOT_X402`, `SERVICE_QUARANTINED`, `SETTLEMENT_UNKNOWN`, `PAYMENT_FAILED`, `PAID_NO_DELIVERY`, `FULFILLMENT_FAILED`, `FULFILLMENT_UNVERIFIED`, `DUPLICATE_SUSPECTED`.

Money is always integer minor units (USDC, 6 decimals).

## 4. Demo sellers (official middleware, no BillGuard code)

One FastAPI app uses `payment_middleware(routes, server)` (`fastapi.py:183-189`). The resource server's facilitator client is chosen by `SELLER_FACILITATOR`:
- **`remote` (default):** `HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))` (`http/facilitator_client_base.py:137-144,155-176`). `FACILITATOR_URL` defaults to `DEFAULT_FACILITATOR_URL` (`http/constants.py:16`).
- **`local`:** the in-process facilitator below.

Network is `eip155:84532`. The middleware calls `get_supported` at startup and exits on a capability mismatch.

| Route | Price | Behavior | Staging |
|---|---|---|---|
| `GET /report/{ticker}` | $0.50 | JSON company record | none |
| `GET /research-slow?topic=` | $0.75 | JSON research result | `on_after_settle` (`server.py:141-144`) runs an `async def` hook. It does `await asyncio.sleep(90)` **only when `ctx.transport_context is not None and ctx.transport_context.request.path == "/research-slow"`** (N1; `.decoded_path` also works). Every other route returns immediately. The hook runs after a successful settle (`server_base.py:1713-1730`). Settlement lands on-chain and the client times out at this route's 15 s `timeout_s`; other routes use 40 s (S2). Never use `time.sleep`, and never let the hook raise: hook exceptions become a 402 settle failure on *every* route (`http/x402_http_server.py:296+`). |
| `POST /convert` `{doc_id, pages}` | $2.00 | PDF built with pypdf | always returns 12 pages |
| `GET /report-premium/{ticker}` | $0.75 | same as report | registry accepts $0.50 → `PRICE_MISMATCH` before signing |

`smoke_sellers.py` uses `x402HttpxClient` with the same `set_spend_controls` override, and checks that `/report` and `/convert` return 200 with a resource (an N1 regression check). `--naive-retry` hits `/research-slow` twice with the plain SDK client and shows two on-chain transfers.

**Public seller.** In hour 1, `bazaar_probe.py` lists CDP Bazaar resources, keeps cheap v2 sellers on `eip155:8453` with USDC, **rejects any whose `extra.assetTransferMethod == "permit2"`** (S6; Permit2 needs an on-chain approve and defeats `authorizationState`), compares `asset` and `payTo` case-insensitively, and smoke-tests two of them with `x402HttpxClient(timeout=60)` plus the `set_spend_controls` override. The winner is registered with the `non_empty` and `content_hash` validators. v1 sellers are out of scope.

**Fallback facilitator (Sugg. 3).** The SDK ships no facilitator HTTP app, so the fallback runs in-process. `sellers/local_facilitator.py` is about 20 lines:

```python
signer = FacilitatorWeb3Signer(private_key=os.environ["FACILITATOR_KEY_SEPOLIA"],
                               rpc_url="https://sepolia.base.org")    # mechanisms/evm/signers.py:264
fac = x402Facilitator()                                               # facilitator.py:43
register_exact_evm_facilitator(fac, signer, networks="eip155:84532")  # mechanisms/evm/exact/register.py:103
server = x402ResourceServer(fac)   # fac satisfies FacilitatorClient (server_base.py:281-302)
```

- The facilitator key needs Base Sepolia ETH for gas (CDP faucet). `setup_wallets.py` checks it. The buyer needs no ETH.
- `FacilitatorWeb3Signer` is synchronous, so settle blocks the seller loop for a few seconds. That is acceptable.
- Transactions stay on-chain, so reconciliation still works. The sellers still import nothing from BillGuard.
- Build it and run `x402ResourceServer(fac).initialize()` once in hour 1, even if x402.org works. The review verified this path by execution.
- The pure stub facilitator exists in pytest only.

## 5. Reconciliation and verification

**Reconciliation** (`reconcile.py`) applies to `SETTLEMENT_UNKNOWN` rows. It is triggered right after an ambiguous outcome, on a repeated intent or a duplicate hit, by `reconcile_purchase`, and by startup recovery. It runs outside the lock.

1. Read `from`, `nonce`, `validBefore` and the encoded header from `payment_payload` evidence.
2. **Poll** `authorizationState(from, nonce)` via web3.py with `AUTHORIZATION_STATE_ABI` (`mechanisms/evm/constants.py:432-444`). Poll up to 5 times, 2 s apart, and stop at the first `true` (Sugg. 2).
   - Sepolia: RPC `https://sepolia.base.org`, USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e`.
   - Mainnet: RPC `https://mainnet.base.org`, USDC `0x8335...2913`.
3. Interpret the result:
   - **`true`** → `SETTLED`. Store evidence `chain_check` with the block number. With no artifact → `PAID_NO_DELIVERY`.
   - **`false` and the block timestamp at the same `block_identifier` is `> validBefore + 30 s`** (S5: chain time, not local clock) → `PAYMENT_FAILED`. This is proven, so the same key may create `attempt+1` (section 3, step 1).
   - **`false` and still valid** → **resend the stored identical header, at most once per tool call** (Sugg. 1a). Store evidence `resend`. The resend is classified by the step 6 rules **without re-entering reconcile**:
     - 2xx with `success=true` → `SETTLED` and store the artifact.
     - Anything else, including a 402 whose `PAYMENT-REQUIRED.error` looks like `invalid_exact_evm_nonce_already_used` (`constants.py:331`) → **one more chain poll** (up to 5 × 2 s, Sugg. 1b). Then `SETTLED` if `true`; otherwise the row stays `SETTLEMENT_UNKNOWN` and the response shows `valid_before`.
     - The error string only triggers the re-poll. It never decides a money outcome, since the TypeScript facilitator may word it differently.

The resend is safe:
- A consumed nonce gets a fresh 402 with no resource (`http/x402_http_server_base.py:552-582`, `mechanisms/evm/exact/facilitator.py:153,354-375`).
- A pending settle hits the `settlement_pending` fast path (`facilitator.py:428-445`).
- It can never double-charge.

`validBefore` is now plus `maxTimeoutSeconds`, about 300 s (`mechanisms/evm/exact/client.py:96-100`, `server_base.py:715`).

Verified: the exact scheme uses EIP-3009, not Permit2, for USDC on both Base networks (`mechanisms/evm/exact/client.py:86-115`, `default_assets.py:30-46`). Tx-hash lookup via Transfer logs is cut.

**Validators** (`verify.py`) are hard-coded functions, each returning `(name, passed, detail)`:
- `json_schema(schema)`
- `ticker_matches` (checks `args["ticker"]`)
- `pdf_page_count` (checks `args["pages"]`)
- `content_type(expected)`
- `non_empty`
- `content_hash`

Any validator exception → `FULFILLMENT_UNVERIFIED`. Each failure creates a `FULFILLMENT_FAILED` finding that points at the validator evidence and the artifact evidence.

## 6. MCP tool surface (FastMCP, stdio)

**Paid tools** are four hand-written typed functions with parameters `(<service args>, purchase_intent_id, task_id=None)`. **None of them has a `force_new_payment` parameter** (N2). Each calls the shared `purchase(service_id, args, purchase_intent_id, task_id)`:
- `get_company_report`
- `get_research_report`
- `convert_document`
- `<public_seller_tool>`

**Response:**
- `purchase_id`, `attempt`, `supersedes_purchase_id`
- `payment_status` (plus `IN_PROGRESS`)
- `history`, for example `[SENT, SETTLEMENT_UNKNOWN(timeout 15s), chain_check(true, block N), SETTLED]` (Sugg. 7)
- `settled_amount`, `currency`, `network`, `settlement_tx`
- `valid_before`, when unknown
- `fulfillment_status`, `verification`, `findings`
- `artifact`: inline JSON, or for PDFs a local path plus page count and hash, never base64
- `safe_next_actions` (vocabulary in section 3; never pay or retry)

**Admin tools:**
- `list_services`, `get_spending_policy`, `get_purchase`, `list_findings(unresolved_only=true)`, `reconcile_purchase`. `reconcile_purchase` never creates a payment; its only network actions are the chain poll and the single identical-header resend.
- There is no MCP tool that can approve a second payment (S8).

Tool descriptions ask the agent to reuse `purchase_intent_id` and check `findings` before spending. The duplicate guard and the missing force parameter enforce this regardless.

## 7. Testing

**Pytest (trimmed, Sugg. 10)** runs against the SQLite repository and covers:
- one case per transition, including `ABANDONED` and compare-and-set loss
- each step 6 outcome branch
- the single-resend bound (the resend outcome does not recurse)
- the duplicate guard
- `attempt+1` after `PAYMENT_FAILED`
- a schema test that no paid tool exposes `force_new_payment` and that no tool named `approve_retry` is registered
- the duplicate beat in both variants: same intent ID and new intent ID both yield `DUPLICATE_SUSPECTED` and no payment (S1)
- `SERVICE_QUARANTINED` after an unresolved `PAID_NO_DELIVERY` (S3)
- each validator
- one hook unit test: with `HTTPRequestContext(adapter=DummyAdapter(), path="/report", method="GET")` the hook returns at once, and with `/research-slow` it sleeps (patched)

Seller-in-process tests with a stub facilitator are skipped. The hour-1 Sepolia smoke covers them.

**Manual:** one Sepolia run per scenario, then a cold-start Claude session with the spec prompt.

## 8. Overnight build order (checkpoint each)

1. **Hour 1, in parallel:**
   - **Track A:** wallets, Sepolia faucet USDC, and Sepolia ETH for the facilitator key. Bring the seller app up (proves `/supported`). `smoke_sellers.py` pays `/report` and `/convert` and logs settle latency (S2). Write `local_facilitator.py` and run `initialize()` once.
   - **Track B:** send USDC to the mainnet wallet (USDC only), then run `bazaar_probe.py` and pick two v2 candidates.
   - **Checkpoint:** one real Sepolia settlement. Not done by 2 h → `SELLER_FACILITATOR=local`.
2. **Core purchase flow.** SQLite repository, state machine, `/report` through BillGuard in Claude Desktop.
   - **Checkpoint:** Claude buys a report.
3. **Mandate and idempotency.** Mandate, duplicate guard (both variants, S1), service quarantine (S3), lock, compare-and-set, startup recovery, `/report-premium`.
   - **Checkpoint:** a `PRICE_MISMATCH` rejection, and a fresh intent ID gets `DUPLICATE_SUSPECTED` with no payment.
4. **Conversion.** `/convert`, PDF validator, findings.
   - **Checkpoint:** `FULFILLMENT_FAILED` with evidence.
5. **Reconciliation.** `/research-slow` (N1 gate), poll, single resend, `history`.
   - **Checkpoint:** `SETTLEMENT_UNKNOWN` → chain → `SETTLED` + `PAID_NO_DELIVERY`. The retry is refused, and `safe_next_actions` contains no payment action.
   - **Record the backup video now.**
6. **Supabase flip.** Implement `store/supabase.py`, create the schema, set `BILLGUARD_STORE=supabase`, and re-run the step 5 checkpoint. Timebox to 45 min; if it misses, stay on SQLite.
7. **Bazaar mainnet purchase,** timeboxed to 1 h. Written fallback: show the discovery listing and the probe output, and say "the payment code path is identical."
8. **Stretch:** the Supabase table view as a live ledger, the `--naive-retry` contrast, and `approve_retry.py` (about 15 lines over `purchase()`). If step 5 lands after about 03:00, the Supabase flip in step 6 also moves here.

**`reset_demo.py` (Sugg. 4).**
- It deletes the configured principal's `purchases`, `evidence` and `findings` on the active store. This also clears the 15-min duplicate guard and the per-principal spend.
- It prints the remaining USDC balances.
- It refuses to run unless `--yes` is passed.
- Run it **between every rehearsal and as the last step before the demo**. Without it, the first research call hits `DUPLICATE_SUSPECTED` and the stall never shows.
- The $5 budget is per reset. One full run spends about $3.25 on Sepolia plus the Bazaar price, so two unreset runs give `BUDGET_EXCEEDED`.
- On-chain transfers are not undone. Only BillGuard's ledger is reset.

**Cut:** Armature, Stripe, Transfer-log lookup, ledger UI work, validator DSL, seller-in-process tests, agent-facing forced retry, and the `approve_retry` MCP tool.

Steps 3-5 are independent once step 2 lands.

## 9. Demo script (6 minutes)

Pre-demo: run `reset_demo.py --yes`.

1. Hook: "Agents can already spend money. Who checks they got what they paid for?"
2. `smoke_sellers.py`: these are ordinary x402 endpoints, and `sellers/app.py` has no BillGuard import. Optionally run `--naive-retry` to show the bare SDK paying twice.
3. Claude Desktop, prompt from spec §8, budget $5.
4. Report purchase → `VERIFIED`, with a Sepolia explorer link.
5. Research purchase → timeout → reconciled on-chain within the same call. Show `history`: `SETTLEMENT_UNKNOWN` → `chain_check(true)` → `SETTLED`, `PAID_NO_DELIVERY`.
   - Presenter line: **"Try that research purchase again."**
   - Result: `DUPLICATE_SUSPECTED`, no second payment, `safe_next_actions: [human_review, report_to_seller]`. This holds whether Claude reuses the intent ID (S1) or rewords the topic (S3: `SERVICE_QUARANTINED`).
6. Convert purchase → 12 of 20 pages → `FULFILLMENT_FAILED` with page-count evidence. Claude has no tool to pay again.
7. `list_findings` → Claude summarizes what was spent, what was delivered, and what is unresolved.
8. Bazaar seller on mainnet: "this seller has never heard of us."
9. Limits:
   - no escrow, no refunds
   - schema-valid is not fact-checked
   - a second payment needs a human; the agent has no tool for it

## 10. Open risks

- **Facilitator or RPC flakiness:** switch to `SELLER_FACILITATOR=local`, and play the backup video recorded after step 5. The chain poll absorbs RPC lag.
- **Supabase or Wi-Fi trouble:** stay on `BILLGUARD_STORE=sqlite`. It is already the default.
- **No usable v2 Bazaar seller, or mainnet funding delayed:** use the written fallback from step 7.
- **Stale rehearsal state:** forgetting `reset_demo.py` breaks scenario B. It is on the pre-demo checklist.
- **Hook regression (N1):** `smoke_sellers.py` must show `/report` and `/convert` delivering after any seller change.
