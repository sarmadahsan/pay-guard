# Demo: the seller that delivers half a file

A deliverable that was cut short is the most content-independent failure a buyer can prove: the seller
took the money and the bytes stop before the document does. It happens honestly (a server bug, an
upstream stream that dropped, a proxy timeout) and dishonestly, and it looks the same to a naive buyer,
who stores the file and moves on. PayGuard's `not_truncated` validator runs **first** for every
service, so a cut body is a proven `FULFILLMENT_FAILED` with evidence instead of the weaker
"suspected" `FULFILLMENT_UNVERIFIED` that a later validator tripping over the half-file used to yield.

## What it checks

Every check is recorded in the validator evidence under `checks`, and the artifact evidence carries
`declared_length` (the seller's `Content-Length`, when sent without a content encoding) next to
`received_length`, so the response and the dispute bundle show both numbers.

| Check | Applies to | Reported as |
|---|---|---|
| transport | any body with a `Content-Length` | `1060 of 1767 declared bytes` |
| pdf | `application/pdf` | `no %%EOF`, `no startxref`, `startxref offset N beyond M bytes`, `pypdf: <error>` (trailing whitespace after `%%EOF` is fine) |
| json | `application/json`, `+json` | body must parse; a non-parsing body is `truncated` when brackets/braces are unbalanced or a string is open, otherwise `malformed json (...)` |
| text | text-ish and raw bodies | `trailing NUL padding (N bytes)`, `cut inside a UTF-8 multibyte sequence at byte N` |

The validator never raises. When it fails, `run_validators` stops there: the remaining validators
(for example `pdf_page_count`) would only be reading a half-file, and a raising validator must not
downgrade the proven finding. The finding reads like:

    Validator 'not_truncated' failed: pdf truncated (no %%EOF; no startxref; pypdf: PdfStreamError: Stream has ended unexpectedly; 1060 of 1767 declared bytes)

with `safe_next_actions: [human_review, report_to_seller]`. The payment is `SETTLED`; nothing is
paid twice.

## Run it

The demo seller has an opt-in switch. In `.env` (never committed):

```
SELLER_TRUNCATE_PATHS=/convert
```

Restart the seller (`uv run uvicorn sellers.app:app --port 8402`). Every *settled* 2xx response
whose path starts with a listed prefix (comma-separated: `/convert,/report`) is cut to 60% of its
bytes after the x402 middleware has settled the payment, with a `Content-Length` that matches the
cut bytes. Then, from Claude Desktop:

> Convert document d1 to a 12-page PDF with PayGuard.

`convert_document` comes back 🔴: `SETTLED` + `FULFILLMENT_FAILED`, "pdf truncated (no %%EOF; ...)",
artifact `received_length` well below the honest file size, and the path of the half PDF on disk for
the dispute. `SELLER_TRUNCATE_PATHS=/report` does the same for JSON (`json truncated (unterminated
string)`). With the variable unset nothing is installed and `scripts/smoke_sellers.py` prints
`SMOKE OK` as before.

### Why there is no "lying Content-Length" variant

Sending the cut body under the original `Content-Length` would also fire the transport check, but
uvicorn refuses it on both of its HTTP backends (`RuntimeError: Response content shorter than
Content-Length`) and the buyer sees a closed connection (`httpx.RemoteProtocolError`), which PayGuard
already classifies as `SETTLEMENT_UNKNOWN` and reconciles. The transport check is therefore covered
by the fake seller in `tests/conftest.py` (mode `truncated`), not by the live demo seller.
