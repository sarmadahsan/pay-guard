VERDICT: NEEDS_REVISION

## Summary Assessment

v2 fixes C1-C4 correctly, and its SDK citations hold up. That includes a live check that x402.org lists `exact`/`eip155:84532` v2. Two new defects remain, both cheap to fix:
- The scenario B hook reads an attribute that does not exist. Because the hook is global, every paid route settles on-chain and then returns a 402.
- `force_new_payment` is a parameter the agent controls, and BillGuard advertises it as a "safe next action." Claude can therefore pay twice in scenarios B and C without a human involved.

SDK paths below are relative to `x402probe/lib/python3.14/site-packages/x402/`.

## Critical Issues (must fix)

### N1. `ctx.transport_context.request.url.path` raises `AttributeError`, so every paid route returns 402 after the money moves
- In the FastAPI middleware, `transport_context.request` is an `HTTPRequestContext`, not a Starlette `Request`:
  - `fastapi.py:331` builds it as `HTTPTransportContext(request=context)`, where `context` is the `HTTPRequestContext` from `fastapi.py:270-278`.
  - That dataclass has `adapter`, `path`, `method`, `payment_header`, `route_pattern` and `decoded_path`. It has no `url` (`http/types.py:80-99`).
  - **Verified by execution:** `AttributeError: 'HTTPRequestContext' object has no attribute 'url'`.
- `on_after_settle` hooks run on every route, and the hook evaluates the path check on every call. The hook runs *after* a successful facilitator settle (`server_base.py:1713-1730`), and hook exceptions are not caught in the driver (`server.py:286-298`).
  - The exception is caught at `http/x402_http_server.py:296+` and becomes a settle failure. The seller then returns 402 with `success=false`.
  - Result: `/report` and `/convert` both settle on-chain and deliver nothing. BillGuard would reconcile these as `SETTLED` + `PAID_NO_DELIVERY`, so scenarios A and C turn into scenario B.
- **Fix:** gate on `ctx.transport_context is not None and ctx.transport_context.request.path == "/research-slow"`. `.decoded_path` also works. Round 1 cited `.request.path`; v2 changed it.

### N2. `force_new_payment` lets Claude pay twice for the scenario B and scenario C purchases
- After the duplicate guard fires, the response offers `safe_next_actions: ["reconcile_purchase", "force_new_payment=true"]`. `force_new_payment` is permitted when the prior row is `SETTLED` and not verified.
- Those are exactly the scenario B (`PAID_NO_DELIVERY`) and scenario C (`FULFILLMENT_FAILED`) rows.
- Claude has $5 and was told to "purchase what you need." It will plausibly take the advertised action:
  - It pays $0.75 again for `/research-slow`. That route always stalls, so the result is a second `PAID_NO_DELIVERY`.
  - Or it pays $2.00 again for `/convert`, which always returns 12 pages.
- On stage, that means BillGuard approving a second payment for the same failed purchase. This is the headline failure the product exists to prevent.
- It also contradicts the spec: "spending authorization… must be enforced outside the language model" (spec §limits, line ~709).
- The design is back to the C4 problem: the demo depends on what the LLM chooses.
- **Fix (cheapest first):**
  1. Remove `force_new_payment` from the paid tools' MCP schemas. Expose it only through a human path: a CLI `scripts/approve_retry.py <purchase_id>`, or `policy.yaml: allow_agent_forced_retry: false`.
  2. Never list it in `safe_next_actions` for `PAID_NO_DELIVERY` or `FULFILLMENT_FAILED`. Use `"report_to_seller"` / `"human_review"` instead.
  3. Keep the `attempt`/`supersedes_purchase_id` machinery for the human path.

## Suggestions (nice to have)

1. **The resend step is safe and useful as written, but make the bound explicit.**
   - What a replayed `PAYMENT-SIGNATURE` actually does:
     - The middleware re-verifies every request (`http/x402_http_server_base.py:558-582`).
     - The SDK facilitator's `verify` always simulates the transfer (`mechanisms/evm/exact/facilitator.py:153`, `simulate=True`).
     - A consumed nonce maps to `invalid_exact_evm_nonce_already_used` (`facilitator.py:354-375`, `eip3009_utils.py:269-273`, `constants.py:331`).
     - The seller therefore returns a **fresh 402 with `PAYMENT-REQUIRED` carrying that error, no `PAYMENT-RESPONSE`, and no resource**. It does not serve the resource again.
   - So resending a *consumed* authorization is useless. v2 resends only after `authorizationState == false`, which already makes it "reconcile first, then decide." Keep that.
   - In that state, a resend can deliver the resource, for example if the first request died before settle or the facilitator returned 502 before broadcast.
   - If settle is pending in the mempool, the second broadcast reverts on the used nonce. It can never double-charge.
   - Two tightenings:
     - (a) At most **one** resend per tool call. Do not run reconcile recursively from the resend's outcome, or "402 → unknown → reconcile → false → resend" can loop.
     - (b) Treat a resend 402 whose `PAYMENT-REQUIRED.error == invalid_exact_evm_nonce_already_used` as a reason to re-poll `authorizationState` (up to ~10 s) before giving up. Expect it to become `SETTLED`.
   - The real x402.org facilitator is TypeScript and could word the error differently. Do not branch on the string for money decisions; only use it to trigger a re-poll.

2. **Poll briefly before deciding in scenario B.** After the 15 s client timeout, poll `authorizationState` 3-5 times, 2 s apart, before falling through to resend. RPC lag on load-balanced `sepolia.base.org`, or a slow facilitator receipt wait, would otherwise send the demo down the resend branch. That branch is safe but ends in `SETTLEMENT_UNKNOWN` instead of the scripted `SETTLED`.

3. **Use an in-process facilitator fallback; it needs no HTTP server.**
   - The SDK ships no facilitator HTTP app; there are no `/verify`, `/settle` or `/supported` routes anywhere in the package. A "self-hosted facilitator at `FACILITATOR_URL`" therefore means writing three FastAPI routes that match `HTTPFacilitatorClient`'s wire format (`http/facilitator_client_base.py:198-209`).
   - `x402Facilitator` has async `verify`, `settle` and `get_supported`, which matches the `FacilitatorClient` protocol (`server_base.py:281-302`). Pass it directly to the resource server: `x402ResourceServer(fac)`.
   - **Verified by execution:**
     - `FacilitatorWeb3Signer(private_key=..., rpc_url="https://sepolia.base.org")`
     - `register_exact_evm_facilitator(fac, signer, networks="eip155:84532")` (`mechanisms/evm/exact/register.py:103`)
     - `get_supported()` lists `eip155:84532 exact v2`, and `x402ResourceServer(fac).initialize()` succeeds.
   - Requirements: the facilitator key needs a little **Base Sepolia ETH** for gas, from the CDP faucet; the buyer needs none. Add `ETH` to `setup_wallets.py` checks.
   - `FacilitatorWeb3Signer` is synchronous web3, so settle blocks the seller's event loop for a few seconds per purchase. That is acceptable.
   - Sellers still import nothing from BillGuard, and this is about 20 lines behind a `SELLER_FACILITATOR=local` env switch. Prepare it in hour 1 even if x402.org works. **Live check today:** `GET https://x402.org/facilitator/supported` lists `{"x402Version":2,"scheme":"exact","network":"eip155:84532"}`.

4. **Reset between rehearsals is load-bearing; specify `reset_demo.py`.**
   - Rows persist in Supabase. A second rehearsal within 15 min hits `DUPLICATE_SUSPECTED` on the *first* research call, so the stall never shows.
   - The budget also accumulates: two runs at $3.25 each exceed $5, giving `BUDGET_EXCEEDED`.
   - `reset_demo.py` should rotate `principal_id`, or delete that principal's purchases, evidence and findings. Run it as the last pre-demo step.

5. **Startup recovery should not stomp on a live process.**
   - If two server processes share Supabase, process B's recovery can flip process A's in-flight `AUTHORIZED` row to `PAYMENT_FAILED`, which frees the budget, while A signs and sends. This can happen through Claude Desktop launching twice, or a manual run alongside it.
   - Fixes:
     - Only recover rows whose `updated_at` is older than about 60 s.
     - Make every state write a compare-and-set: `update ... where payment_status = <expected>`. A lost race then aborts before signing.
   - Do recovery reconciles in a background task after the MCP `initialize` handshake. Network calls, including a possible resend, should not delay server start.
   - Use a distinct terminal state such as `ABANDONED` (or a reason code) rather than `PAYMENT_FAILED`. The `PAYMENT_FAILED` finding is defined as proven.

6. **Same-key semantics after `PAYMENT_FAILED`.** Step 1 says "same `(principal, intent, fingerprint)` → return latest attempt." After a proven `PAYMENT_FAILED`, the same intent ID then returns failure forever. State whether that auto-creates `attempt+1` (safe, because failure is proven) or requires the human path from N2.

7. **Make the scenario B transition visible.** Reconciliation happens inside the same tool call, so Claude never sees `SETTLEMENT_UNKNOWN` as a status. The spec's expected verdict is "`SETTLEMENT_UNKNOWN` until reconciliation, followed by the actual verified outcome." Include a `history: [SENT, SETTLEMENT_UNKNOWN(timeout 15s), chain_check(true, block N), SETTLED]` array in the response so the audience sees it. Also, Claude may not retry on its own after a clear `SETTLED`/`PAID_NO_DELIVERY`. Script the presenter line ("try again") for the duplicate beat.

8. **The lock scope around reconcile/resend.** Do not hold the process-wide `asyncio.Lock` across the resend (up to 15 s). Release it after marking the row and resend outside it. A concurrent duplicate resend is harmless because the nonce is single-use.

9. **The fingerprint guard does not block legitimate demo repeats.** It only matches in-flight, `SETTLEMENT_UNKNOWN`, or settled-but-unverified rows, so a second `VERIFIED` report purchase (or a different ticker) goes through. This is fine. Just note that an identical *verified* purchase with a fresh intent ID pays again by design.

10. **Overnight plan: further cuts.**
    - Implement **SQLite first** and add the Supabase repository in step 7 (stretch). Two repositories plus a schema sync before the first end-to-end checkpoint is the largest avoidable time sink, and SQLite removes the Wi-Fi dependency from the core path.
    - Trim pytest to state-machine transitions, the outcome branches and the duplicate guard. Skip seller-in-process tests with a stub facilitator; the hour-1 Sepolia smoke covers that.
    - The mainnet buyer needs only USDC, no ETH, because the facilitator pays gas for EIP-3009. That makes Track B funding a single transfer.
    - The rest of the order is realistic for about 10 h if steps 3-5 are parallelized as stated.

11. **`detect_payment_required_version` raises `ValueError`** when neither header nor body identifies a version, for example when the route is unprotected and returns 200 (`http/utils.py:80-114`). Map this to `REJECTED` (`NOT_X402`) before any signing.

## Verified Claims

- **C1 fixed.**
  - `set_spend_controls(controls: SpendControls | Literal[False])` is at `client_base.py:462-469`.
  - Passing a dict with `max_amount_per_payment: False` sets `usd_limit = False` (`client_base.py:601-605`). The default-asset allowlist still applies (`client_base.py:574-599`), and canonical USDC passes it.
  - Passing bare `False` skips all controls (`client_base.py:555-556`). Either form works.
  - `x402HttpxClient(x402_client)` wraps the configured client (`http/clients/httpx.py:344-367`), so `smoke_sellers.py` inherits the setting.
  - The signing exception is now mapped to `REJECTED`.
- **C2 fixed.** `FacilitatorConfig` is a dataclass with `url`, `timeout`, `http_client`, `auth_provider` and `identifier` (`http/facilitator_client_base.py:137-144`). The non-dict branch reads `config.url` (`http/facilitator_client_base.py:155-176`), so `HTTPFacilitatorClient(FacilitatorConfig(url=...))` is correct.
- **C3 fixed.**
  - Only 2xx with decoded `success=true` takes the fast path.
  - A 402 carrying `unexpected_settle_error` (`fastapi.py:441-467`), a 502 (`fastapi.py:439-440`), and a 2xx with no header all go to `SETTLEMENT_UNKNOWN` plus reconcile.
  - `PAYMENT_FAILED` now requires `authorizationState == false && now > validBefore`.
- **C4 fixed in mechanism.** The fingerprint guard is independent of intent ID, and `attempt`/`supersedes` give a forced retry a row. The policy gap is N2.
- **The after-settle hook is awaited after settlement and before the response.** `on_after_settle` is at `server.py:141-144`. It is yielded after a successful settle (`server_base.py:1713-1730`) and awaited via `_execute_hook` (`server.py:297, 374-379`). It runs inside `process_settlement` (`http/x402_http_server.py:242-249`), before `fastapi.py:427-437` returns the body. An `async` sleep stalls delivery while the transfer is already on-chain. Hook exceptions turn into a 402 settle failure (see N1).
- **Replay of a consumed authorization returns 402, not the resource.**
  - Every request with a header is re-verified (`http/x402_http_server_base.py:552-575`), and an invalid result returns a fresh `PAYMENT-REQUIRED` 402 (`http/x402_http_server_base.py:563-582`).
  - Facilitator verify simulates (`mechanisms/evm/exact/facilitator.py:153, 338-375`) and reports `invalid_exact_evm_nonce_already_used` (`constants.py:331`, `eip3009_utils.py:269-273`).
  - Settle also re-verifies before broadcast (`facilitator.py:449-460`).
  - The facilitator has a `settlement_pending` fast path for an identical payload (`facilitator.py:428-445`). The server retries settle once on `settlement_pending` (`server_base.py:330-385`). This prevents a second broadcast in the receipt-timeout case.
- **The in-process SDK facilitator works.** See Suggestion 3. `x402Facilitator.verify/settle(payload, requirements, payload_bytes=None, requirements_bytes=None)` matches the `FacilitatorClient` protocol, and the probe ran `x402ResourceServer(fac).initialize()` successfully.
- **x402.org facilitator supports Base Sepolia exact v2.** Live `GET /facilitator/supported` today (also `upto` and `batch-settlement`; signer `0xd407…f1bf`).
- **`validBefore` and the deadline window.** `validBefore = now + max_timeout_seconds` (`mechanisms/evm/exact/client.py:96-100`), and the server default is 300 (`server_base.py:715`).
- **`detect_payment_required_version`** checks the `PAYMENT-REQUIRED` header first, then `X-PAYMENT`, then body `x402Version`, and raises `ValueError` otherwise (`http/utils.py:80-114`).
