VERDICT: NEEDS_REVISION

## Summary Assessment

The architecture is sound and most SDK claims hold up. The exact scheme uses EIP-3009 on Base Sepolia, the payload exposes nonce/validBefore/from, the middleware settles after the handler, and an awaited after-settle hook makes the scenario B stall possible from inside an ordinary seller. However, two items in the design will fail at runtime as written: the SDK's default $1 per-payment spend cap blocks the $2.00 conversion purchase, and `HTTPFacilitatorClient("url")` crashes. Two parts of the state machine also contradict the product's core "never pay twice / timeout is not failure" promise. All four fixes are small.

SDK paths below are relative to `x402probe/lib/python3.14/site-packages/x402/`.

## Critical Issues (must fix)

### C1. The SDK's default spend cap ($1) rejects the $2.00 `/convert` purchase (scenario C), and also breaks `smoke_sellers.py`
- `client_base.py:184` sets `DEFAULT_MAX_AMOUNT_PER_PAYMENT: Money = "$1"`. `client_base.py:601-605` applies it whenever `max_amount_per_payment` is not explicitly `False`, and `client_base.py:645,670-676` raises `NoMatchingRequirementsError`.
- **Verified by execution.** On a fresh `x402Client()` with `ExactEvmScheme` on `eip155:84532`, a 500000-unit requirement signs fine. A 2000000-unit requirement raises `NoMatchingRequirementsError: All payment requirements were rejected by spend_controls.max_amount_per_payment ($1, including USDC)`. After `client.set_spend_controls({"max_amount_per_payment": False})` it signs with `value=2000000`.
- **Fix:** BillGuard owns the mandate, so construct every `x402Client` with `set_spend_controls({"max_amount_per_payment": False})`, or with the mandate's per-purchase cap as a `"$x"` string as a second safety net. `smoke_sellers.py` must pass the same setting to `x402HttpxClient`, or the "sellers work without BillGuard" stage beat fails on `/convert`.
- Also define how a signing-time exception, which happens before `PAYLOAD_SIGNED`, is recorded. The error rules only cover exceptions after `PAYLOAD_SIGNED`. It should become `REJECTED`/`PAYMENT_FAILED` with no money at risk, never `SETTLEMENT_UNKNOWN`.

### C2. `HTTPFacilitatorClient("https://x402.org/facilitator")` raises `AttributeError`
- `http/facilitator_client_base.py:155-176` accepts only a `FacilitatorConfig` or a `dict`. A `str` falls into the `else` branch and calls `config.url`. **Verified:** `AttributeError: 'str' object has no attribute 'url'`.
- **Fix:** use `HTTPFacilitatorClient(FacilitatorConfig(url=...))` or `HTTPFacilitatorClient({"url": ...})`. Or pass nothing: `DEFAULT_FACILITATOR_URL` is already `https://x402.org/facilitator` (`http/constants.py:16`).

### C3. "402 / 4xx after send → `PAYMENT_FAILED`" is unsafe, and "2xx without `PAYMENT-RESPONSE`" is undefined
- The official FastAPI middleware returns **402 with a `PAYMENT-RESPONSE` header (`success=false`, `error_reason="unexpected_settle_error"`)** whenever settlement raises an unexpected exception (`http/middleware/fastapi.py:441-467`).
  - This includes the case where the facilitator call times out while the transaction lands on-chain. The facilitator HTTP timeout is 90 s (`http/facilitator_client_base.py:27`).
  - A `FacilitatorResponseError` returns 502 (`fastapi.py:439-440`), again with the on-chain outcome unknown.
  - So a parsed failure header does **not** prove that money did not move. Under the design's rule, BillGuard would mark `PAYMENT_FAILED` ("safe to pay again") and could then pay twice. That directly violates spec acceptance criterion 5.
- A 2xx with no `PAYMENT-RESPONSE` header (a non-SDK Bazaar seller, a proxy stripping headers, or a decode error) appears in no outcome branch.
- **Fix (cheap):**
  - After `SENT`, the only fast path is 2xx with a decoded `success=true`. Every other outcome goes to `SETTLEMENT_UNKNOWN` and is immediately reconciled in the same call with `authorizationState`.
  - Use `PAYMENT_FAILED` only when `authorizationState == false` **and** either `now > validBefore`, or the seller's response shows the request died at *verify* (a fresh 402 carrying `PAYMENT-REQUIRED`/`error`, with no settle header).
  - For a 2xx without a header: store the artifact, reconcile, then verify.

### C4. The idempotency key leaves the scenario B "refuse silent second payment" moment to LLM behavior, and `force_new_payment` contradicts the key
- **The key depends on Claude reusing the ID.** The key is `(principal, purchase_intent_id, fingerprint)`. If Claude retries with a fresh `purchase_intent_id`, which LLMs often do after an error, BillGuard creates a brand-new purchase and pays again. The headline demo beat then depends on Claude following a tool-description instruction.
- **`DUPLICATE_SUSPECTED` has no trigger.** It is listed as a finding code, but no rule produces it.
- **`force_new_payment` cannot work with the unique key.** A second payment for the same key needs a second row, but the unique constraint on the key forbids it. The design does not say where attempt #2 lives.
- **Fix:**
  - Add a **fingerprint guard**: before creating a new row, look for any row with the same `(principal_id, request_fingerprint)` in `{INTENT_RECORDED..SENT, SETTLEMENT_UNKNOWN}`, or `SETTLED` with fulfillment not `VERIFIED`, within a window (e.g. 15 min). If one exists, return it with a `DUPLICATE_SUSPECTED` finding and require `force_new_payment=true`.
  - Add an `attempt` integer to the unique key (or a `supersedes_purchase_id` column) so a forced retry has a row to live in.
  - This makes scenario B deterministic regardless of which intent ID Claude sends.

## Suggestions (nice to have)

1. **Pin the requirement the SDK signs.** `x402Client.create_payment_payload` re-runs selection over `payment_required.accepts` (`client_base.py:773`). The default selector returns `requirements[0]` (`client_base.py:322-327`). If a seller offers multiple accepts, especially a Bazaar seller, the SDK may sign something other than what BillGuard authorized.
   - Pass `payment_required.model_copy(update={"accepts": [authorized_req]})`.
   - After signing, assert `payload.accepted == authorized_req` and `payload.payload["authorization"]["value"] == authorized_amount`.
   - Also check `asset` equals the canonical USDC for the network (`mechanisms/evm/default_assets.py:30-46`). Otherwise `amount > accepted_price_minor` compares units of a different token.
2. **Persist the full encoded `PAYMENT-SIGNATURE` header at `PAYLOAD_SIGNED`, not just its fields.** Resending the identical EIP-3009 authorization is inherently idempotent, because the nonce is single-use on-chain. For `SETTLEMENT_UNKNOWN` with `authorizationState=false` and still valid, BillGuard can then safely *resend the same payload* instead of refusing. The signature is a bearer authorization limited to that `payTo`/amount/window, so storing it in the DB is acceptable. Clarify that "secrets never in evidence" means private keys.
3. **Concurrency.** Claude Desktop can issue parallel tool calls ("buy these three").
   - The MCP server is a single process, so wrap intent-creation plus authorize in one `asyncio.Lock`. That closes the budget read-then-write race.
   - Back it with a DB unique constraint.
   - A losing call that finds an in-flight row (`INTENT_RECORDED..SENT`) should return `IN_PROGRESS` and must not reconcile or pay.
   - With supabase-py, `upsert(..., ignore_duplicates=True)` returns empty data on conflict, so follow it with a select.
4. **Define the budget set explicitly.** "AUTHORIZED..SETTLED plus SETTLEMENT_UNKNOWN" is ambiguous given the branching graph. Spent = sum of `authorized_amount_minor` over `{AUTHORIZED, PAYLOAD_SIGNED, SENT, SETTLEMENT_UNKNOWN, SETTLED}`; `REJECTED` and `PAYMENT_FAILED` are excluded.
5. **Startup recovery.** On MCP server start:
   - Rows in `AUTHORIZED` (no payload signed) → `PAYMENT_FAILED` (no money at risk; frees budget).
   - Rows in `PAYLOAD_SIGNED`/`SENT` → `SETTLEMENT_UNKNOWN`.
   - Claude Desktop restarts the stdio server freely, so this will happen during rehearsal.
6. **Scenario B seller stall: feasible as designed, with caveats.** Make the hook `async def` with `await asyncio.sleep(90)`. A synchronous `time.sleep` blocks the single uvicorn event loop and stalls every other seller route mid-demo. Gate it on `ctx.transport_context.request.path` (or `.adapter.get_query_param("stall")`); `transport_context` is populated (`fastapi.py:336,394,404`). Better still, use a dedicated path such as `/research-slow`, so the registry entry is clean. The "proxy fallback" is not needed.
7. **The fallback facilitator should not be a stub.** A stub facilitator puts nothing on-chain, so `authorizationState` returns false and scenario B's reconciliation story collapses. The fallback should be a self-hosted SDK facilitator (`facilitator.py:43 x402Facilitator` + `mechanisms/evm/signers.py:264 FacilitatorWeb3Signer` + `mechanisms/evm/exact/facilitator.py:88`) with a Sepolia-ETH-funded key. It is still real chain, so reconcile still works. Keep a pure stub only for pytest.
8. **Bazaar on mainnet is the riskiest item; move its discovery to hour 1.**
   - Many Bazaar sellers may still be x402 v1 (402 JSON body / `X-PAYMENT`). The design's manual flow decodes only the v2 `PAYMENT-REQUIRED` header. Either filter candidates to v2, or use `detect_payment_required_version` (`http/utils.py:80-114`) and support v1.
   - Funding a mainnet wallet with real USDC can take an hour on the night.
   - Pick two candidates and smoke-test them with `x402HttpxClient` early.
9. **Store choice.** Supabase adds a venue-Wi-Fi dependency to every state transition. SQLite (local file) is faster to build and makes "store unreachable → fail before signing" trivial. If Supabase is kept for sponsor fit, consider SQLite as the source of truth plus a best-effort Supabase mirror for the live-ledger view. supabase-py's default client is synchronous, so blocking calls inside an async FastMCP handler are acceptable for a demo but should be noted.
10. **FastMCP dynamic tools.** FastMCP derives schemas from function signatures, so generating tools from registry `input_schema` at runtime takes more effort than writing 4 small typed functions that call a shared `purchase(service_id, args, intent_id, ...)`. Hand-write them.
11. **PDF artifacts.** Do not return base64 PDFs in tool output; they bloat Claude's context. Return a local path plus page count and hash.
12. **The "with and without BillGuard" contrast (spec §8 hook).** Optionally run `smoke_sellers.py` against the stall route with a naive retry, to show the SDK auto-client would pay twice. This is the most persuasive 20 seconds available, and it costs about 10 lines.
13. **Validity window.** `validBefore = now + maxTimeoutSeconds` (`mechanisms/evm/exact/client.py:100`), and the server default is 300 s (`server_base.py:715`). The "refused until deadline" window is therefore about 5 minutes. Show the deadline in the response; it is a good demo line.
14. **Time plan (about 10 h, 1-2 people).** Feasible if cut.
    - **Must keep:** steps 1-5.
    - **Cut first:** Armature, Stripe, Transfer-log tx lookup, live ledger UI, and the generic validator DSL. Hard-code `$input.pages`/`$input.ticker` resolution.
    - **Timebox:** the Bazaar mainnet step (6) to 1 h, with a written fallback ("here is the discovery listing; payment code path is identical").
    - **Record the backup video right after step 5 works,** not at the end.

## Verified Claims

- **Exact EVM scheme uses EIP-3009 (not Permit2) for USDC on eip155:84532.** `mechanisms/evm/exact/client.py:86-88` uses Permit2 only when `extra.assetTransferMethod == "permit2"`; otherwise `client.py:97-115` builds an EIP-3009 `transferWithAuthorization`. The Base Sepolia default asset `0x036CbD53842c5426634e7929541eC2318f3dCF7e` (name "USDC", version "2", 6 decimals) has no `asset_transfer_method` (`default_assets.py:39-46`). The same holds for Base mainnet `0x8335...2913` (`default_assets.py:30-37`). The Permit2 contingency in §5 can be dropped for these assets.
- **The signed payload exposes the reconcile inputs.** `x402Client.create_payment_payload` is async (`client.py:173`) and returns a `PaymentPayload` with `.payload` (dict) and `.accepted` (`schemas/payments.py:92-96`). `.payload["authorization"]` has `from`, `to`, `value`, `validAfter`, `validBefore`, `nonce` (`mechanisms/evm/types.py:32-40`). **Verified by execution:** `from` equals the signer address, `nonce` is 32-byte hex, and `validBefore` is an int string.
- **The `authorizationState` ABI ships with the SDK.** `mechanisms/evm/constants.py:432-444` defines `AUTHORIZATION_STATE_ABI`, which can be reused in `reconcile.py`. web3 8.0.0 is already installed as a dependency.
- **Reconcile semantics are correct.** EIP-3009 nonces are random per authorization (`client.py:97`). A `true` therefore proves this exact authorization was consumed, since the buyer never calls `cancelAuthorization`. `false` with `now > validBefore` proves it can never settle.
- **Header helpers are available for the manual state machine.** In `http/utils.py`:
  - `encode_payment_signature_header` / `decode_payment_signature_header` (28-44)
  - `decode_payment_required_header` (54-65)
  - `decode_payment_response_header` → `SettleResponse` (74-77)
  - All are base64 of camelCase JSON.
  - Header names are `PAYMENT-SIGNATURE`, `PAYMENT-REQUIRED` and `PAYMENT-RESPONSE` (`http/constants.py:4-6`).
  - `SettleResponse` has `success`, `error_reason`, `payer`, `transaction`, `network` and `amount` (`schemas/responses.py:75-81`).
  - Signature header round trip verified by execution.
- **The middleware settles after the handler.** `fastapi.py:339` runs `call_next` first. Responses ≥400 cancel settlement (`fastapi.py:360-378`). The body is buffered and then `process_settlement` is awaited (`fastapi.py:381-406`). The response with settlement headers is returned only after that (`fastapi.py:427-437`).
- **An after-settle hook exists, is awaited, and runs before the response is sent.** `x402ResourceServer.on_after_settle` is at `server.py:141-144`. After-settle hooks are yielded after a successful facilitator settle (`server_base.py:1713-1730`) and awaited via `_execute_hook`, which awaits coroutines (`server.py:298, 374-379`). This all happens inside `process_settlement` (`http/x402_http_server.py:244-250`), before `fastapi.py:432` builds the response. An async sleep there delays delivery after on-chain settlement, so the scenario B stall is achievable with official middleware.
- **Default facilitator.** `DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"` (`http/constants.py:16`). The middleware calls the facilitator's `/supported` at startup and exits on fatal capability mismatches (`fastapi.py:249-257`, `http/background_init.py:28-41`). A missing `eip155:84532` would therefore fail loudly at seller start, which is a good early check. Actual Sepolia support by x402.org cannot be confirmed from source alone; run the seller once in hour 1.
- **`EthAccountSigner` exists** (`mechanisms/evm/signers.py:68`). `ExactEvmScheme` also auto-wraps a raw `LocalAccount` (`client.py:27-38,66`).
