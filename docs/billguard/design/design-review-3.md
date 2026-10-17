VERDICT: APPROVED

## Summary Assessment

N1 and N2 are fixed. I verified N1 against the SDK. The money-safety claims also hold:
- The order handler → settle (with receipt wait) → `on_after_settle` → response is confirmed.
- A replayed identical header cannot cause a second transfer.
- `PAYMENT_FAILED` → `attempt+1` is safe because of the EIP-3009 `validBefore` check.

I found no defect that pays twice, loses money, or breaks the scripted demo. Three items should still be fixed during implementation because they affect demo fidelity:
- **S1:** a same-intent retry returns no `DUPLICATE_SUSPECTED`, which contradicts PRD metric 4.
- **S2:** the 15 s timeout applies to every route.
- **S3:** a scenario B retry with reworded arguments is not caught by the guard.

SDK paths are relative to `x402probe/lib/python3.14/site-packages/x402/`.

## Critical Issues (must fix)

None.

## Suggestions (nice to have)

**S1. A same-intent retry does not yield `DUPLICATE_SUSPECTED`, which contradicts PRD §7 metric 4 and demo step 5. Fix before build; it is about two lines.**
- Design §3 step 1: if a row exists for `(principal, intent, fingerprint)` and it is `SETTLED`/`PAID_NO_DELIVERY`, the call falls into "Anything else → return it". The duplicate guard only runs "otherwise", that is, for a *new* intent ID.
- The tool descriptions tell Claude to reuse `purchase_intent_id`. The spec's demo step 3 says the agent retries "using the same purchase intent". The most likely "try again" therefore returns the old row with no `DUPLICATE_SUSPECTED` finding. Money is safe, but the scripted beat and the acceptance criterion fail.
- **Fix:** when a same-key hit lands on a row that would trip the guard (in-flight, `SETTLEMENT_UNKNOWN`, or settled-not-verified), attach the same `DUPLICATE_SUSPECTED` finding and `history` entry. Do this once per row, or append a `retry_blocked` evidence row on each hit, so findings do not multiply.
- Add a pytest that covers both the same-intent and the new-intent variants.

**S2. One 15 s client timeout for every route can turn a slow but successful `/report` (or the Bazaar buy) into `PAID_NO_DELIVERY`.**
- The seller settles synchronously before responding, and the exact facilitator waits for the receipt (`mechanisms/evm/exact/facilitator.py:531-545` → `settle_receipt.py:105`). The seller's facilitator client waits up to 90 s (`http/facilitator_client_base.py:27`).
- Suppose x402.org verify + settle takes more than 15 s on a bad night. BillGuard then times out, polls, sees `true`, and records `SETTLED` with no artifact. The identical-header resend cannot fetch the resource, because the nonce is consumed and the seller returns a fresh 402. Scenario A would show as scenario B.
- **Fix:** make the timeout a per-registry field. Use 15 s for `/research-slow` and about 40 s for the others; the 90 s stall still forces the timeout in B. In hour 1, log the measured settle latency from `smoke_sellers.py`.

**S3. The duplicate guard is an exact match on `canonical_json(args)`. If Claude rewords the research topic, it pays $0.75 again and gets a second `PAID_NO_DELIVERY`.**
- This is not paying twice for the same request. On stage, though, it reads as "BillGuard let the agent pay again for the thing that didn't arrive."
- **Fix (cheap and pitchable):** quarantine the *service*. While a service has an unresolved `PAID_NO_DELIVERY` finding, reject new purchases from it with `SERVICE_QUARANTINED` and `safe_next_actions: [human_review]`.
- Failing that, normalize free-text args (lowercase, collapse whitespace) before fingerprinting.
- Scenario C needs nothing: a second $2 convert exceeds the remaining $1.75 → `BUDGET_EXCEEDED`.

**S4. Keep worst-case tool latency under Claude Desktop's MCP request timeout (about 60 s by default in the TS MCP client).**
- The worst path today: quote + 15 s send + about 10 s poll + 15 s resend + about 10 s re-poll, roughly 50-55 s. The happy path for B is about 17 s.
- Cap the resend timeout at about 8 s, or put a 45 s wall-clock budget on the whole call. After that budget, return `SETTLEMENT_UNKNOWN` with `reconcile_purchase` in `safe_next_actions`.

**S5. Prove `PAYMENT_FAILED` using chain time, not local time.** Read `authorizationState` and the block timestamp at the same `block_identifier`, and require `block.timestamp > validBefore` (plus a small margin). This removes the corner case where a lagging load-balanced RPC node, together with local clock skew, marks a just-included transfer as failed.
- The underlying safety claim itself is true:
  - USDC's EIP-3009 rejects an authorization once `block.timestamp >= validBefore`.
  - The SDK client sets `validAfter=0` and `validBefore = now + maxTimeoutSeconds` (`mechanisms/evm/exact/client.py:95-100`). The server default is 300 s (`server_base.py:715`).
  - So after expiry, the first authorization can never land, however slow the facilitator is.
- Bazaar sellers may advertise a larger `maxTimeoutSeconds`. The fallback is 3600 (`client.py:98`), so their unknown window can be an hour.

**S6. The Bazaar probe should reject sellers that use `extra.assetTransferMethod == "permit2"`.**
- The exact client switches to Permit2 on that flag (`mechanisms/evm/exact/client.py:86-88`).
- Permit2 needs a prior on-chain approve, which costs ETH the mainnet wallet will not have.
- Reconcile uses `authorizationState`, which does not apply to Permit2.
- Step 4's `payload["authorization"]["value"]` assertion would fail on such a seller. That fails safe as `REJECTED/SIGNING_FAILED`, but you would only discover it on stage.
- Also:
  - Compare `asset` and `payTo` case-insensitively.
  - Give `bazaar_probe.py` the same `set_spend_controls({"max_amount_per_payment": False})` override. The SDK default cap is `$1` (`client_base.py:184`).
  - Give the probe and smoke clients an explicit `timeout=60`. `x402HttpxClient` is a plain `httpx.AsyncClient` (`http/clients/httpx.py:344-367`), so it inherits httpx's 5 s default. A settle slower than 5 s makes the smoke "fail" after paying.
  - `--naive-retry` must catch the `PaymentError` that wraps `ReadTimeout` (`http/clients/httpx.py:194-197`).

**S7. MCP and stdio details:**
- **Protect stdout.** Any `print`, web3 warning or supabase-py log on stdout corrupts the JSON-RPC stream. Configure root logging to stderr at startup.
- **Background recovery.** FastMCP has no "after initialize" callback. Spawn the recovery task from the lifespan with `asyncio.create_task` (optionally `sleep(2)` first). That does not block the handshake, and it is sufficient.
- **PDF artifacts as a local path.** Returning a path is acceptable for the demo. The verdict rests on `page_count` and hash, which are inline. Claude Desktop cannot open the path without a filesystem MCP, so do not script Claude "reading" the PDF. Tool results are small (inline JSON reports), so size is not an issue.
- **Two concurrent processes on SQLite.** Claude Desktop sometimes spawns a server twice. Open SQLite in WAL mode with `busy_timeout` (for example 5000 ms). Otherwise a second process's recovery can raise `database is locked`.
  - That failure is fail-closed before signing. After `SENT`, it leaves the row for recovery, which is correct but noisy.
  - The in-process `asyncio.Lock` does not span processes. The cross-process guard race needs simultaneous identical calls to two instances, which is unrealistic, and the PRD already lists it as a non-goal.

**S8. `approve_retry` as an MCP tool is not "human-only."** When `BILLGUARD_ALLOW_FORCE=1`, Claude can call it; the "operator use only" description is not enforcement. Drop the admin-tool variant and keep only the CLI. This also removes one item from the build.

**S9. Small text and PRD alignment fixes:**
- **`create_payment_payload` is `async`** on `x402Client` (`client.py:173`). Await it.
- **The hook unit test needs more arguments.** `HTTPRequestContext` requires `adapter` and `method` (`http/types.py:80-86`), so the test must pass a dummy adapter and `"GET"`.
- **PRD F10 is missing the 60 s staleness rule.** It should say "rows older than 60 s, compare-and-set."
- **The limits slide references a stretch item.** Demo step 9 names `approve_retry.py`, which is stretch in §8. Either build the 15-line CLI or reword the slide to "a human must approve."
- **PRD §8 Q5.** The 15-min window is fine given `reset_demo.py`. Close the question.

**S10. Overnight plan.** It is realistic for 1-2 people if you hold the timeboxes. Move these to stretch or cut them:
- the `approve_retry` admin tool (S8)
- the Supabase flip, which is already timeboxed; skip it entirely if step 5 lands after about 03:00
- the `content_hash` validator (`non_empty` is enough for the Bazaar seller)
- the `wait_until` action

Keep the local facilitator, but timebox it to 20 min in hour 1. The faucet ETH is the slow part, so request it first.

## Verified Claims

**N1 fixed.**
- The design text (§0, §4 table) gates on `ctx.transport_context is not None and ctx.transport_context.request.path == "/research-slow"`.
- `HTTPRequestContext` has `adapter, path, method, payment_header, route_pattern, decoded_path` and no `url` (`http/types.py:80-90`).
- The FastAPI middleware builds it with `path=raw_path` (the query is stripped via `split("?")[0]`) and `decoded_path` (`http/middleware/fastapi.py:268-278`). It wraps it as `HTTPTransportContext(request=context)` (`fastapi.py:331`) and passes that to `process_settlement` (`fastapi.py:398-406`).
- That reaches `settle_payment(..., transport_context=...)` (`http/x402_http_server.py:243-249`). It is stored on `SettleResultContext.transport_context` (`schemas/hooks.py:308,313`; `server_base.py:1710-1719`).
- `.path == "/research-slow"` therefore matches for `/research-slow?topic=x` and never matches `/report/...` or `/convert`.

**N2 fixed in text.**
- No paid tool has `force_new_payment` (§6).
- `safe_next_actions` has no pay or retry value (§3 vocabulary).
- `approve_retry` is registered only when `BILLGUARD_ALLOW_FORCE=1`, which defaults to 0, and a pytest asserts the schema.
- Caveat: see S8.

**Seller order is handler → settle (receipt awaited) → `on_after_settle` → response.**
- `call_next` runs the handler first (`fastapi.py:339`).
- The body is buffered (`fastapi.py:381-383`), then `process_settlement` is awaited (`fastapi.py:398`).
- Inside it, the facilitator `settle` is yielded first (`server_base.py:1666-1670`). On success, the after-settle hooks are yielded (`server_base.py:1710-1726`) and awaited by `_execute_hook` (`server.py:372-377`). The response is built only afterwards (`fastapi.py:428-437`).
- The exact facilitator's settle broadcasts and then waits for the receipt (`mechanisms/evm/exact/facilitator.py:529-540` → `settle_receipt.py:105`).
- So in scenario B the transfer is mined a few seconds after send, well before the 15 s client timeout. The first `authorizationState` poll should return `true`, and the 5 × 2 s poll covers RPC lag.
- The async `sleep(90)` only delays the response. If a disconnect cancels the sleep, settlement is already final.

**A replay of the identical header cannot cause a second on-chain transfer.**
- Every request with a header is re-verified, and verify simulates (`facilitator.py:145-153`). A consumed nonce maps to a revert reason (`facilitator.py:354-375`), so the middleware returns a fresh 402 without running the handler.
- If the nonce is not yet consumed (the first settle is pending), a resend to `/research-slow` *does* run the handler and settle again:
  - In the Python facilitator, the pending-store fast path reuses the first tx (`facilitator.py:428-445`). Otherwise the re-verify fails, or the second broadcast reverts on the used nonce. The EIP-3009 nonce is single-use on-chain.
  - At worst the facilitator wastes gas; the buyer is never charged twice.
- The server's one automatic settle retry only fires on `settlement_pending` (`server_base.py:377-386`).

**Hook exceptions turn into a 402 settle failure.** `x402_http_server.py:296-310` catches them, and so does the `fastapi.py:441-467` fallback. The design's "never let the hook raise" warning is correct.

**`PAYMENT_FAILED` → `attempt+1` is safe.** See S5 for the chain-time refinement. The budget excludes `PAYMENT_FAILED`/`ABANDONED`, which is correct because no transfer can still land.

**Signing domain.** If `extra` lacks `name`/`version`, the client falls back to `DEFAULT_ASSETS` (`mechanisms/evm/exact/client.py:261-268`). Base mainnet USDC is `"USD Coin"/"2"` and Sepolia is `"USDC"/"2"` (`mechanisms/evm/default_assets.py:30-46`). A Bazaar seller that omits `extra` still signs correctly.

**`x402Client.create_payment_payload` re-selects from `accepts`** (`client_base.py:773`) and sets `accepted=selected` (`client_base.py:837-843`). The design's `model_copy(update={"accepts":[selected]})` plus the `payload.accepted == selected` assertion is correct.

**Spend controls.** `controls is False` bypasses all filtering (`client_base.py:555-556`). The default cap is `"$1"` (`client_base.py:184`), so the override is required for the $2 `/convert`.

**`x402HttpxClient` adds no auto-retry beyond one payment and one hook-driven recovery** (`http/clients/httpx.py:120-191`). It is not on BillGuard's path, since BillGuard sends the header itself.
