# Demo: the agent files the dispute itself

**Pitch in one line:** the buyer agent files, the seller's agent replies, PayGuard keeps the thread as evidence.

Every PayGuard finding that proves a loss (`PAID_NO_DELIVERY`, `FULFILLMENT_FAILED`, `PRICE_MISMATCH`) ends
with `safe_next_actions` of `human_review`, `report_to_seller`. Two admin tools implement `report_to_seller`:

- `file_dispute(purchase_id)` emails the seller a plain-text complaint from the agent's own inbox on
  [Primitive](https://primitive.dev), with `payguard-evidence.json` attached (purchase, findings, quote,
  on-chain settlement proof, the delivered artifact's hash, the failed checks; the signed payment
  authorization is redacted). It records the sent-mail id as `dispute` evidence and appends
  `DISPUTE_FILED` to the purchase history. One dispute per purchase: a second call returns the first.
- `check_dispute(purchase_id)` asks Primitive for the reply, records it as `dispute_reply` evidence (body
  capped at 4000 chars), appends `DISPUTE_REPLY`, and returns it with `safe_next_actions: ["human_review"]`.
  A reply never resolves the finding and nothing here can pay: the schema test still classifies both as
  admin tools with no force/retry parameter.

Filing moves no money, so the agent may do it unprompted. PayGuard arbitrates nothing; it documents.

## Automatic disputes

**The protection is a policy, not a prompt.** With `auto_dispute: true` in `policy.yaml` (the shipped
default for the demo), the agent never has to decide to complain: whenever a purchase ends with an
unresolved proven `PAID_NO_DELIVERY`, `FULFILLMENT_FAILED` or `PRICE_MISMATCH` finding, PayGuard files
the dispute itself, inside the same tool call, through the same code path as `file_dispute`. The row the
paid tool returns already carries it:

- `dispute: {filed_by: "policy", status: "awaiting_reply", ...}` and the headline ends `· dispute filed`;
- the history ends `FULFILLMENT_FAILED` → `DISPUTE_FILED {filed_by: policy, dispute_filed: true}`, so a
  judge can see it happened in-call, after the finding and before the agent saw anything;
- `safe_next_actions` gains `check_dispute` (reading the reply is free and safe), still never pay or retry.

Rules the hook keeps:

- **One dispute per purchase.** A retry of the same `purchase_intent_id` (the `DUPLICATE_SUSPECTED` path),
  a later `reconcile_purchase`, startup recovery and a manual `file_dispute` all find the dispute on record
  and file nothing new.
- **The outcome never changes.** If the send fails (bad Primitive key, no seller contact, timeout), payment
  and fulfillment status stay exactly what the checks proved; the failure is logged and recorded as
  `seller_error` evidence `{reason: "auto_dispute failed", ...}`, and the agent can still call `file_dispute`.
  One automatic attempt per purchase.
- **The 45 s call budget wins.** With fewer than 5 s left the hook skips, logs, and records the same
  `auto_dispute failed` evidence (a terminal row gets no later pass), so `report_to_seller` stays in
  `safe_next_actions` and the agent can call `file_dispute`. Nothing in the hook can create a payment.
- **A refusal is not a payment.** For `PRICE_MISMATCH` the email reports the quoted price against the agreed
  one, says no money moved, and asks the seller to quote the agreed price; only paid rows ask for a refund.
- **One dispute per purchase, even under concurrency.** The check, the send and the ledger write hold a
  per-purchase lock, so an overlapping `file_dispute` and auto-dispute cannot both send.
- `get_spending_policy` reports `auto_dispute` next to the budget, so the mandate is inspectable. Set it to
  `false` to go back to agent-initiated disputes only. The loader rejects anything but `true`/`false`.

Where it runs: `run_from_quote` and `reconcile_and_finish` in `payguard/purchase.py` end in
`disputes.auto_dispute(deps, row, deadline)`, so `purchase`, `reconcile_purchase`, startup recovery and
`scripts/approve_retry.py` all benefit. No new env var: the backend is the one `file_dispute` uses (dry run
when `PRIMITIVE_API_KEY` is empty).

## Env

In `.env` (never committed):

```
PRIMITIVE_API_KEY=prim_...                     # empty = dry run (see below)
PRIMITIVE_FROM=buyer@agent.markavy.com         # the buyer agent's Primitive inbox
SELLER_CONTACT_EMAIL=seller@agent.markavy.com  # fallback for sellers without /.well-known/x402-seller.json
```

Recipient resolution, in order: `GET {seller origin}/.well-known/x402-seller.json` → `support_email`,
then `SELLER_CONTACT_EMAIL`. Meridian Intel on :8403 publishes `support_email=seller@agent.markavy.com`;
PayGuard's own demo sellers on :8402 publish nothing and fall back to the env value, so every dispute in
the demo lands in the same inbox. That inbox is a second Primitive agent inbox the operator controls, so
its reply comes back through Primitive and `check_dispute` finds it by the sent-email id (never by sender
address): `GET /sent-emails/{dispute_id}/reply`.

**Dry run.** With `PRIMITIVE_API_KEY` empty, `file_dispute` renders the same email and writes it to
`$PAYGUARD_ARTIFACT_DIR/disputes/<purchase_id>.txt` (headers, body, attachment) and returns
`dispute_id: local-<purchase_id>`, `backend: dry_run`. Drop a `<purchase_id>.reply.txt` next to it and
`check_dispute` reads it back as the reply, so the whole beat rehearses offline.

Restart the PayGuard MCP server after editing `.env`.

## The prompts

With `auto_dispute: true` the Meridian beat (`docs/pay-guard/demo/meridian.md`) already ends with the
dispute filed: the `get_intel_report` result shows `dispute.filed_by: "policy"`, the headline ends
`· dispute filed`, and `history[-1]` is `DISPUTE_FILED`. The prompts below are the manual path
(`auto_dispute: false`, or a seller whose contact could not be resolved in-call):

1. > File a dispute for every unresolved finding.

   The agent calls `list_findings`, then `file_dispute` per purchase. Expected result per purchase:
   `dispute_id`, `to` = `seller@agent.markavy.com` (Meridian's `support_email` from its well-known file), `subject`
   `Dispute: intel purchase <id> (0.500000 USDC)`, a 400-char `body_preview`, `evidence_count`,
   `explorer_url`. `get_purchase` now shows `dispute: {status: "awaiting_reply", reply_count: 0}` and the
   headline ends `· dispute filed`. A verified purchase is refused with `error: ... no unresolved proven
   finding to dispute`.

2. Later, once the seller's agent has answered from `seller@agent.markavy.com`:

   > Check whether Meridian replied.

   `check_dispute` returns `status: "replied"` with the reply's sender, time and text, the headline ends
   `· seller replied`, and the finding stays open with `human_review` as the only next action.

## What the judge sees

- An email written by one agent to another: the facts, the Base Sepolia transaction link as proof, and
  the ask (deliver, or refund the settled amount to the buyer address), with the evidence attached.
- The seller's reply landing in PayGuard's ledger as evidence next to the 402 body and the
  `AuthorizationUsed` event, visible in `get_purchase` and `list_purchases`.
- No second payment anywhere in the flow. The dispute tools cannot spend; the human decides.

Covered offline by `tests/test_disputes.py` (fake seller + fake Primitive over `httpx.MockTransport`),
including the `auto_dispute` hook: on, off, verified purchase, failing sender, duplicate call, reconcile path.
The Primitive API shape was verified against `https://www.primitive.dev/openapi.json` on 2026-10-17:
`POST /send-mail` (single `to` address, inline `attachments[{filename, content_type, content_base64}]`)
and `GET /sent-emails/{id}/reply` (one best reply or `null`).
