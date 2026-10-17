# BillGuard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use engineering:subagent-driven-development (recommended) or engineering:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the BillGuard overnight-hackathon MVP: an MCP server that buys from ordinary x402 v2 sellers, enforces a spending mandate before signing, reconciles ambiguous settlements on-chain, verifies deliveries, and never lets the agent pay twice without a human.

**Architecture:** One `uv` monorepo with three processes: the FastMCP stdio server (`billguard/`), the FastAPI demo sellers using only the official x402 middleware (`sellers/`), and Claude Desktop. The purchase flow is an explicit state machine persisted by compare-and-set before every network call, behind a `Repository` protocol with SQLite (default) and Supabase backends. Reconciliation reads EIP-3009 `authorizationState` on the USDC contract with web3.py.

**Tech Stack:** Python 3.12, uv, `x402==2.25.0` (v2 protocol, `[fastapi,extensions]` extras), `fastmcp` 2.x, FastAPI + uvicorn, httpx, web3.py, eth-account, pypdf, PyYAML, python-dotenv, supabase-py, pytest + pytest-asyncio.

**Authority:** `docs/billguard/design/design-final.md` (sections 3, 5, 6, 8 govern behavior and sequencing), `docs/billguard/prd/prd.md` (F1–F13, metrics 1–8), `docs/billguard/design/design-review-3.md` (build-time gotchas S1–S10). This plan does not redesign anything. Where the design leaves an implementation detail open, the choice is marked **Plan decision** and stays inside the design's constraints.

---

## 0. How to use this plan

### 0.1 Structure

- **Milestones M0–M9** follow design §8 exactly: hour-1 wallets/sellers/smoke (tracks A and B), core purchase flow, mandate + idempotency + quarantine, conversion + validators, reconciliation, Supabase flip, Bazaar seller, stretch. M9 is the pre-demo checklist (§9) and the fallbacks (§10) as explicit tasks.
- **Every milestone** has: a timebox with a wall-clock window, a **cut list** (what to drop if it overruns, matching design §8 and review S10), the **checkpoint named in §8**, and a **manual verification list** the operator runs before declaring the milestone done.
- **Every task** has: workstream tag, requirement mapping (PRD F-ids, metric numbers, design sections), timebox, files (create/modify/test), SDK calls, design §7 tests it covers, the manual Sepolia check, an **implementer brief** (the prompt a less capable agent is given), bite-sized steps with complete code, and **automatically verifiable exit criteria** (commands plus expected output).
- Tasks are numbered `<milestone>.<n>`. Task text is self-contained: the controller pastes the full task plus §0.4 into the implementer prompt and nothing else is required.

### 0.2 Workstream tags (parallelism)

| Tag | Meaning | Can run in parallel with |
|---|---|---|
| `[W-ENV]` | Repo scaffold, dependencies, env | nothing (first) |
| `[W-SELLERS]` | Hour-1 Track A: wallets, seller app, local facilitator, smoke | `[W-BAZAAR]` |
| `[W-BAZAAR]` | Hour-1 Track B: mainnet USDC, Bazaar probe | `[W-SELLERS]` |
| `[W-CORE]` | M2 core purchase flow and MCP server | nothing (gate for M3–M5) |
| `[W-MANDATE]` | M3 guard, quarantine, recovery, schema tests | `[W-VERIFY]`, `[W-RECONCILE]` |
| `[W-VERIFY]` | M4 PDF artifact, validators, findings | `[W-MANDATE]`, `[W-RECONCILE]` |
| `[W-RECONCILE]` | M5 seller stall hook, chain poll, resend, history | `[W-MANDATE]`, `[W-VERIFY]` |
| `[W-SUPABASE]` | M6 Supabase repository | `[W-MAINNET]` |
| `[W-MAINNET]` | M7 Bazaar seller registration and purchase | `[W-SUPABASE]` |
| `[W-STRETCH]` | M8 stretch items | each other |
| `[W-DEMO]` | M9 pre-demo checklist, rehearsal, fallback drills | nothing (last) |

**Parallel rule for M3, M4, M5 (design §8: "Steps 3–5 are independent once step 2 lands"):** each owns a disjoint set of files (table in §0.3). M2 creates every module with its final public interface, so M3–M5 fill bodies and add tests without touching each other's files. If a task finds it must edit a file outside its ownership row, it stops and reports `BLOCKED` rather than editing.

### 0.3 File ownership

| Path | Created in | Owned by (may modify after creation) |
|---|---|---|
| `pyproject.toml`, `.python-version`, `.env.example`, `policy.yaml`, `README.md` (run section) | M0 | `[W-ENV]`, then M7 appends Bazaar env |
| `billguard/__init__.py`, `billguard/logging_setup.py` | M0 | `[W-ENV]` |
| `billguard/models.py` | M2 | `[W-CORE]` only |
| `billguard/store/__init__.py`, `store/base.py`, `store/sqlite.py` | M2 | `[W-CORE]`; M6 edits `store/__init__.py` only to add the Supabase branch |
| `billguard/store/supabase.py` | M6 | `[W-SUPABASE]` |
| `billguard/registry.py` | M2 | `[W-CORE]`; M7 edits only the Bazaar entry block |
| `billguard/policy.py`, `billguard/wallet.py` | M2 | `[W-CORE]` |
| `billguard/artifacts.py` | M2 (JSON + bytes) | `[W-VERIFY]` (adds PDF page count) |
| `billguard/verify.py` | M2 (4 basic validators) | `[W-VERIFY]` (adds `pdf_page_count`, `content_hash`) |
| `billguard/guard.py` | M2 (stub) | `[W-MANDATE]` |
| `billguard/recovery.py` | M2 (stub) | `[W-MANDATE]` |
| `billguard/reconcile.py` | M2 (stub) | `[W-RECONCILE]` |
| `billguard/chain.py` | M5 | `[W-RECONCILE]` |
| `billguard/outcome.py`, `billguard/response.py`, `billguard/purchase.py`, `billguard/deps.py` | M2 | `[W-CORE]` only (M3–M5 never edit these) |
| `billguard/server.py` | M2 | `[W-CORE]`; M7 adds the Bazaar tool block only |
| `sellers/app.py` | M1 (routes, no stall) | `[W-SELLERS]`, then `[W-RECONCILE]` adds the stall hook in M5 |
| `sellers/local_facilitator.py` | M1 | `[W-SELLERS]` |
| `sellers/stall.py` | M5 | `[W-RECONCILE]` |
| `scripts/setup_wallets.py`, `scripts/smoke_sellers.py` | M1 | `[W-SELLERS]` |
| `scripts/bazaar_probe.py` | M1 | `[W-BAZAAR]` |
| `scripts/reset_demo.py` | M2 | `[W-CORE]` |
| `scripts/approve_retry.py` | M8 | `[W-STRETCH]` |
| `tests/conftest.py`, `tests/test_models.py`, `tests/test_store_sqlite.py`, `tests/test_policy.py`, `tests/test_purchase_flow.py` | M2 | `[W-CORE]` |
| `tests/test_verify.py` | M2 (basic cases) | `[W-VERIFY]` appends |
| `tests/test_guard.py`, `tests/test_recovery.py`, `tests/test_schema.py` | M3 | `[W-MANDATE]` |
| `tests/test_artifacts.py` | M4 | `[W-VERIFY]` |
| `tests/test_reconcile.py`, `tests/test_seller_hook.py`, `tests/test_scenario_b.py` | M5 | `[W-RECONCILE]` (5.4 runs only after M3 merged) |
| `tests/test_store_supabase.py`, `supabase/schema.sql` | M6 | `[W-SUPABASE]` |
| `tests/test_bazaar_tool.py` | M7 | `[W-MAINNET]` |
| `docs/billguard/demo/` (checklist, fallback slide, video path) | M9 | `[W-DEMO]` |

### 0.4 Standing instructions for every implementer (paste with every task)

```
You are implementing one task of the BillGuard hackathon plan. Read the task text in full before acting.

Hard rules (violations fail review):
1. Never commit secrets. Private keys come from environment variables (loaded from a gitignored `.env`). Never print a private key. Never write a private key into evidence, logs, tests, or docs.
2. `sellers/app.py` and `sellers/local_facilitator.py` import NOTHING from `billguard`. `grep -n billguard sellers/` must return nothing.
3. In `billguard/` never write to stdout. stdout carries MCP JSON-RPC. Use `logging` (configured to stderr by `billguard/logging_setup.py`). Scripts under `scripts/` may print to stdout.
4. Money is always integer minor units (USDC has 6 decimals). Never use floats for amounts.
5. Every purchase state transition goes through `Repository.update_status(purchase_id, expected, new, ...)` (compare-and-set). Never write `payment_status` any other way.
6. No agent-facing tool may take a force/retry parameter, and no MCP tool named `approve_retry` may exist. `safe_next_actions` values are only: get_purchase, reconcile_purchase, wait_until:<ts>, human_review, report_to_seller, use_artifact.
7. Only edit files in your task's "Files" list. If you believe you must edit another file, stop and report BLOCKED with the reason.
8. Follow TDD where the task shows a test first: write the test, run it and see it fail, implement, run it and see it pass, commit.
9. Use `uv run pytest ...` and `uv run python ...` for every command. Do not create a venv by hand.
10. Commit after each task with the message given in the task. Do not push.

Environment facts you can rely on:
- Python 3.12 via uv; the x402 SDK is 2.25.0. Public import paths used by this plan (all verified against the installed SDK):
  from x402.client import x402Client
  from x402.server import x402ResourceServer
  from x402.facilitator import x402Facilitator
  from x402.http import HTTPFacilitatorClient, FacilitatorConfig
  from x402.http.clients.httpx import x402HttpxClient, PaymentError
  from x402.http.middleware.fastapi import payment_middleware
  from x402.http.types import HTTPRequestContext, HTTPTransportContext, RouteConfig, PaymentOption
  from x402.http.constants import PAYMENT_REQUIRED_HEADER, PAYMENT_SIGNATURE_HEADER, PAYMENT_RESPONSE_HEADER, DEFAULT_FACILITATOR_URL
  from x402.http.utils import detect_payment_required_version, decode_payment_required_header, encode_payment_required_header, encode_payment_signature_header, decode_payment_response_header, encode_payment_response_header
  from x402.mechanisms.evm.exact import ExactEvmScheme, ExactEvmServerScheme
  from x402.mechanisms.evm.exact.register import register_exact_evm_facilitator
  from x402.mechanisms.evm.signers import EthAccountSigner, FacilitatorWeb3Signer
  from x402.mechanisms.evm.constants import AUTHORIZATION_STATE_ABI
  from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS
  from x402.schemas import PaymentRequired, PaymentRequirements, PaymentPayload, SettleResponse
  from x402.schemas.payments import ResourceInfo
  from x402.extensions.bazaar import with_bazaar, ListDiscoveryResourcesParams   # needs x402[extensions]
- `x402Client.create_payment_payload` is `async`; await it. Its result has `.accepted` (PaymentRequirements) and `.payload["authorization"]` with keys from, to, value, validAfter, validBefore, nonce.
- `HTTPRequestContext(adapter, path, method, payment_header=None, route_pattern=None, decoded_path=None)`; it has no `.url`.
- `x402ResourceServer.initialize()` is synchronous. `x402ResourceServer.on_after_settle(hook)` registers an async hook taking one `SettleResultContext` with `.transport_context.request` being an `HTTPRequestContext`.
- Canonical USDC: eip155:84532 → 0x036CbD53842c5426634e7929541eC2318f3dCF7e (EIP-712 name "USDC", version "2"); eip155:8453 → 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 (name "USD Coin", version "2").
- The SDK client's default spend cap is "$1"; BillGuard and the smoke scripts disable it with client.set_spend_controls({"max_amount_per_payment": False}).

When done, report: Status (DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT), what you implemented, the exact exit-criteria commands you ran and their output, files changed, and any concern.
```

### 0.5 Exit-criteria conventions

- `Run:` lines are executed from the repo root. `Expected:` states the observable result. A task is done only when every `Run:` produces its `Expected:`.
- Pytest node ids are exact. `uv run pytest -q` must stay green at the end of every task (no skipped tests except Supabase/live ones that are gated on env).
- **Manual Sepolia check** lines are for the operator, not the implementer agent, unless the task says the agent runs them. They cost real test USDC and need `.env`.

### 0.6 Overnight schedule (assumes kickoff T0 = 21:00; shift all times if T0 differs)

| Milestone | Window | Timebox | Hard rule |
|---|---|---|---|
| M0 scaffold | 21:00–21:20 | 20 min | |
| M1 hour 1 (A ∥ B) | 21:20–22:20 | 60 min | Checkpoint not met by **23:00 (T+2h)** → `SELLER_FACILITATOR=local` (design §8.1) |
| M2 core flow | 22:20–00:20 | 2 h | |
| M3 mandate ∥ M4 conversion ∥ M5 reconciliation | 00:20–02:00 (3 people) or 00:20–03:30 (solo, in order M3 60 min → M4 45 min → M5 90 min) | | Record the backup video at the end of M5 (§8.5). **Solo note:** the solo schedule lands M5 at 03:30, so by the 03:00 rule the Supabase flip is stretch for a solo builder; only a 2–3 person team reaches M6 in its slot. |
| M6 Supabase flip | after M5, 45 min | 45 min | **If M5 lands after 03:00, M6 moves to stretch** (§8.8, S10) |
| M7 Bazaar mainnet | after M6 or M5, 60 min | 60 min | Miss → written fallback (§8.7) |
| M8 stretch | 05:15–06:00 (or what remains) | 45 min | Cut in reverse order: 8.3, then 8.2, then 8.1 |
| M9 demo prep | 06:00–07:00 | 60 min (9.1: 25 min, 9.2: 25 min, 10 min slack) | `reset_demo.py --yes` is the last command before the demo |

---

## Milestone M0 — Repo scaffold `[W-ENV]`

**Timebox:** 20 min (21:00–21:20). **Cut if overrun:** none; this is the floor. If `uv sync` fails on a dependency, pin the failing package to the version listed in the task and continue.
**Checkpoint:** `uv run pytest -q` runs (0 tests) and every third-party import in Task 0.1 step 4 succeeds.

### Task 0.1: Project skeleton, dependencies, env template, policy `[W-ENV]`

**Maps to:** F12 (scripts dir), F13 (store selection env), non-functional "stderr logging", "secrets from env". Design §2 layout.
**Timebox:** 20 min.
**SDK calls:** none.
**§7 tests covered:** none (scaffold).

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.env.example`, `policy.yaml`, `billguard/__init__.py`, `billguard/logging_setup.py`, `billguard/store/__init__.py` (empty for now), `sellers/__init__.py`, `scripts/__init__.py`, `tests/__init__.py`, `tests/conftest.py` (minimal), `artifacts/.gitkeep`
- Modify: `README.md` (append a "Run" section)

**Implementer brief:** You are creating the empty project that every later task builds on. Nothing here talks to a network. Get the dependency set installed with `uv`, prove the imports work, and make the repo safe against secret leaks (`.env` is already gitignored; you add `.env.example` with placeholder values only).

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "billguard"
version = "0.1.0"
description = "Buyer-side purchasing gateway for x402 paid services (hackathon MVP)"
requires-python = ">=3.12"
dependencies = [
  "x402[fastapi,extensions]==2.25.0",
  "fastmcp>=2.3",
  "fastapi>=0.115",
  "uvicorn>=0.30",
  "httpx>=0.27",
  "web3>=7",
  "eth-account>=0.13",
  "pypdf>=5",
  "pyyaml>=6",
  "python-dotenv>=1",
  "supabase>=2.10",
  "jsonschema>=4",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["billguard", "sellers", "scripts"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `.python-version`, `.env.example`, `policy.yaml`**

`.python-version`:
```
3.12
```

`.env.example` (placeholders only; the real `.env` is gitignored):
```bash
# Wallet private keys (0x-prefixed hex). LEAVE EMPTY: scripts/setup_wallets.py generates any empty key.
# NEVER commit the real .env. Never paste a known/test key such as 0x...0001 here: anyone can sweep it.
BUYER_KEY_SEPOLIA=
BUYER_KEY_MAINNET=
FACILITATOR_KEY_SEPOLIA=
SELLER_KEY_SEPOLIA=
SELLER_PAY_TO=

# Sellers
SELLER_BASE_URL=http://127.0.0.1:8402
SELLER_FACILITATOR=remote            # remote | local
FACILITATOR_URL=https://x402.org/facilitator
SELLER_STALL_S=90

# Chain RPC
RPC_URL_SEPOLIA=https://sepolia.base.org
RPC_URL_MAINNET=https://mainnet.base.org

# BillGuard
BILLGUARD_STORE=sqlite               # sqlite | supabase
BILLGUARD_SQLITE_PATH=billguard.db
BILLGUARD_ARTIFACT_DIR=artifacts
BILLGUARD_POLICY=policy.yaml
BILLGUARD_LOG_LEVEL=INFO

# Supabase (M6)
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

# Bazaar seller (M7) — filled from scripts/bazaar_probe.py --pick
BAZAAR_URL=
BAZAAR_PAY_TO=
BAZAAR_PRICE_MINOR=
BAZAAR_CONTENT_TYPE=application/json
BAZAAR_TOOL_NAME=get_public_data
```

`policy.yaml` (design §2 policy fields; $5 budget, $2.50 per-purchase cap so the $2 convert passes once and a second one hits the remaining-budget rule):
```yaml
principal_id: demo-agent
total_budget_minor: 5000000
per_purchase_cap_minor: 2500000
allowed_networks:
  - "eip155:84532"
  - "eip155:8453"
allowed_services:
  - report
  - report_premium
  - research
  - convert
  - bazaar
```

- [ ] **Step 3: Write `billguard/logging_setup.py` and the empty packages**

`billguard/logging_setup.py`:
```python
"""Route every log line to stderr. stdout is reserved for MCP JSON-RPC (review S7)."""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("BILLGUARD_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.captureWarnings(True)
    # Third-party libraries that are chatty at INFO.
    for noisy in ("httpx", "httpcore", "web3", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
```

`billguard/__init__.py`, `billguard/store/__init__.py`, `sellers/__init__.py`, `scripts/__init__.py`, `tests/__init__.py`: empty files.

`tests/conftest.py` (minimal; Task 2.6 replaces it):
```python
"""Shared fixtures. Extended in M2."""
```

`artifacts/.gitkeep`: empty file (the directory is gitignored except this marker; add `!artifacts/.gitkeep` to `.gitignore`).

- [ ] **Step 4: Install and verify imports**

Run: `uv sync --group dev`
Expected: exits 0; `uv.lock` created.

Run: `uv run python -c "import x402, fastmcp, fastapi, httpx, web3, pypdf, yaml, dotenv, supabase, jsonschema; from x402.extensions.bazaar import with_bazaar; print('imports ok', x402.__name__)"`
Expected: `imports ok x402`

Run: `uv run python -c "import importlib.metadata as m; print(m.version('x402'))"`
Expected: `2.25.0`

Run: `uv run pytest -q`
Expected: `no tests ran` with exit code 5 (acceptable at M0 only).

- [ ] **Step 5: README run section**

Append to `README.md`:
```markdown
## Run (hackathon)

    uv sync --group dev
    cp .env.example .env            # then fill keys via scripts/setup_wallets.py
    uv run uvicorn sellers.app:app --port 8402      # demo sellers
    uv run python -m billguard.server               # MCP server over stdio (Claude Desktop launches this)
    uv run pytest -q

Plan: docs/billguard/design/2026-10-17-billguard-implementation-plan.md
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .python-version .env.example policy.yaml billguard sellers scripts tests artifacts/.gitkeep .gitignore README.md
git commit -m "chore: scaffold billguard project with pinned x402 2.25.0"
```

**Exit criteria (auto):**
- Run: `git ls-files | grep -c '^\.env$'` → Expected: `0`
- Run: `uv run python -c "from billguard.logging_setup import configure_logging; configure_logging(); import logging; logging.getLogger('t').info('x')" 2>&1 >/dev/null | grep -c ' INFO t: x'` → Expected: `1` (the line went to stderr, not stdout)
- Run: `uv run python -c "import x402; print('ok')"` → Expected: `ok`

**Manual Sepolia check:** none.

---

## Milestone M1 — Hour 1: wallets, sellers, smoke (Track A) ∥ mainnet + Bazaar probe (Track B)

**Timebox:** 60 min (21:20–22:20). **Hard stop 23:00:** if no real Sepolia settlement by then, set `SELLER_FACILITATOR=local` and move on (design §8.1). **Cut if overrun:** Track B's smoke of candidates (keep only the listing; registration happens in M7). The local facilitator is timeboxed to 20 min inside Track A (S10): request faucet ETH first because that is the slow part.
**Checkpoint (§8.1):** one real Sepolia settlement through `smoke_sellers.py` with settle latency logged.

### Task 1A.1: `scripts/setup_wallets.py` — keys, faucet links, balances `[W-SELLERS]`

**Maps to:** F12, design §2 wallets, §4 fallback facilitator funding, §8.1 Track A.
**Timebox:** 15 min.
**§7 tests covered:** none (manual Sepolia funding precondition).

**Files:**
- Create: `scripts/setup_wallets.py`

**SDK calls:** none from x402. Uses `eth_account.Account.create()` / `Account.from_key()`, `web3.Web3`.

**Implementer brief:** Write a CLI that (1) generates any missing wallet key and appends it to the gitignored `.env` without ever printing it, (2) prints the four addresses and the faucet links, and (3) prints ETH and USDC balances on Base Sepolia and USDC on Base mainnet. `--check` exits non-zero when the demo cannot run (buyer Sepolia USDC below 4 USDC, or, when `SELLER_FACILITATOR=local`, facilitator ETH below 0.0005 ETH). Balances come from public RPCs; tolerate RPC failure by printing `unavailable` and continuing.

- [ ] **Step 1: Write the script**

```python
"""Create or show wallets, print faucet links and balances. Never prints private keys.

Usage:
  uv run python scripts/setup_wallets.py          # create missing keys in .env, show addresses + balances
  uv run python scripts/setup_wallets.py --check  # balances only; exit 1 if the demo cannot run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

USDC_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ERC20_BALANCE_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

KEYS = ["BUYER_KEY_SEPOLIA", "BUYER_KEY_MAINNET", "FACILITATOR_KEY_SEPOLIA", "SELLER_KEY_SEPOLIA"]

FAUCETS = {
    "Base Sepolia ETH (facilitator key only)": "https://portal.cdp.coinbase.com/products/faucet",
    "Base Sepolia USDC (buyer key)": "https://faucet.circle.com",
}


def _ensure_key(name: str, env: dict[str, str]) -> str:
    if env.get(name):
        return env[name]
    acct = Account.create()
    key_hex = Web3.to_hex(acct.key)  # always 0x-prefixed (hexbytes 1.x .hex() is not)
    with ENV_PATH.open("a") as fh:
        fh.write(f"\n{name}={key_hex}\n")
    env[name] = key_hex
    print(f"generated {name} -> {acct.address} (written to .env, not shown)")
    return env[name]


def _ensure_pay_to(env: dict[str, str]) -> str:
    if env.get("SELLER_PAY_TO"):
        return env["SELLER_PAY_TO"]
    addr = Account.from_key(env["SELLER_KEY_SEPOLIA"]).address
    with ENV_PATH.open("a") as fh:
        fh.write(f"SELLER_PAY_TO={addr}\n")
    env["SELLER_PAY_TO"] = addr
    return addr


def _usdc(w3: Web3, token: str, addr: str) -> float | None:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_BALANCE_ABI)
        return c.functions.balanceOf(Web3.to_checksum_address(addr)).call() / 1_000_000
    except Exception:
        return None


def _eth(w3: Web3, addr: str) -> float | None:
    try:
        return w3.eth.get_balance(Web3.to_checksum_address(addr)) / 1e18
    except Exception:
        return None


def _fmt(v: float | None, unit: str) -> str:
    return "unavailable" if v is None else f"{v:.6f} {unit}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="balances only; non-zero exit if the demo cannot run")
    args = ap.parse_args()

    load_dotenv(ENV_PATH)
    env = {k: os.environ.get(k, "") for k in KEYS + ["SELLER_PAY_TO", "SELLER_FACILITATOR"]}
    for k in KEYS:  # refuse publicly known placeholder keys (0x...0001 etc.); they would be swept instantly
        if env.get(k) and set(env[k][2:].lstrip("0")) <= set("0123456789") and len(env[k][2:].lstrip("0")) <= 2:
            print(f"{k} looks like a placeholder key; clearing it so a real one is generated")
            env[k] = ""
    if not args.check:
        if not ENV_PATH.exists():
            ENV_PATH.write_text("# generated by scripts/setup_wallets.py — never commit\n")
        for k in KEYS:
            _ensure_key(k, env)
        _ensure_pay_to(env)
    missing = [k for k in KEYS if not env.get(k)]
    if missing:
        print(f"missing keys in .env: {missing}; run without --check first")
        return 1

    buyer_sep = Account.from_key(env["BUYER_KEY_SEPOLIA"]).address
    buyer_main = Account.from_key(env["BUYER_KEY_MAINNET"]).address
    fac = Account.from_key(env["FACILITATOR_KEY_SEPOLIA"]).address
    pay_to = env.get("SELLER_PAY_TO") or Account.from_key(env["SELLER_KEY_SEPOLIA"]).address

    sepolia_rpc = os.environ.get("RPC_URL_SEPOLIA", "https://sepolia.base.org")  # read after load_dotenv
    mainnet_rpc = os.environ.get("RPC_URL_MAINNET", "https://mainnet.base.org")
    w3s = Web3(Web3.HTTPProvider(sepolia_rpc, request_kwargs={"timeout": 15}))
    w3m = Web3(Web3.HTTPProvider(mainnet_rpc, request_kwargs={"timeout": 15}))

    buyer_usdc = _usdc(w3s, USDC_SEPOLIA, buyer_sep)
    fac_eth = _eth(w3s, fac)
    main_usdc = _usdc(w3m, USDC_MAINNET, buyer_main)
    seller_usdc = _usdc(w3s, USDC_SEPOLIA, pay_to)

    print("addresses")
    print(f"  buyer   (sepolia) {buyer_sep}  USDC {_fmt(buyer_usdc, 'USDC')}")
    print(f"  buyer   (mainnet) {buyer_main}  USDC {_fmt(main_usdc, 'USDC')}  (needs USDC only, no ETH)")
    print(f"  facilitator (sepolia) {fac}  ETH {_fmt(fac_eth, 'ETH')}  (used only when SELLER_FACILITATOR=local)")
    print(f"  seller pay_to (sepolia) {pay_to}  USDC {_fmt(seller_usdc, 'USDC')}")
    print("faucets")
    for label, url in FAUCETS.items():
        print(f"  {label}: {url}")

    ok = True
    if buyer_usdc is not None and buyer_usdc < 4.0:
        print("CHECK FAIL: buyer Sepolia USDC < 4 (one full demo run costs ~3.25)")
        ok = False
    if env.get("SELLER_FACILITATOR", "remote") == "local" and fac_eth is not None and fac_eth < 0.0005:
        print("CHECK FAIL: facilitator Sepolia ETH < 0.0005 while SELLER_FACILITATOR=local")
        ok = False
    print("CHECK OK" if ok else "CHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it once to create the keys, then request faucet funds (operator)**

Run: `uv run python scripts/setup_wallets.py`
Expected: four `generated ... (written to .env, not shown)` lines on first run, then the `addresses` block and `faucets` block. No private key appears in the output.

Operator: open the two faucet links now (ETH for the facilitator address, USDC for the buyer Sepolia address). Request at least 10 test USDC. Send a few dollars of real USDC on Base mainnet to the mainnet buyer address (Track B needs it).

- [ ] **Step 3: Commit**

```bash
git add scripts/setup_wallets.py
git commit -m "feat(scripts): setup_wallets creates keys in .env and prints balances"
```

**Exit criteria (auto):**
- Run: `uv run python scripts/setup_wallets.py | grep -ciE '0x[0-9a-f]{64}'` → Expected: `0` (no 64-hex private key printed)
- Run: `grep -c '^BUYER_KEY_SEPOLIA=0x' .env` → Expected: `1`
- Run: `git status --porcelain | grep -c '\.env$'` → Expected: `0` (`.env` is ignored, not staged)

**Manual Sepolia check:** after the faucet lands, `uv run python scripts/setup_wallets.py --check` prints `CHECK OK`.

### Task 1A.2: Demo sellers with the official middleware + local facilitator `[W-SELLERS]`

**Maps to:** F11 (routes, zero BillGuard imports), non-functional facilitator default and `SELLER_FACILITATOR=local`, metric 1, design §4.
**Timebox:** 25 min (local facilitator part: 20 min max, S10).
**§7 tests covered:** "Seller-in-process tests are skipped; the hour-1 Sepolia smoke covers them."

**Files:**
- Create: `sellers/app.py`, `sellers/local_facilitator.py`

**SDK calls:** `x402ResourceServer(facilitator_client)`, `server.register("eip155:84532", ExactEvmServerScheme())`, `payment_middleware(routes, server)`, `HTTPFacilitatorClient(FacilitatorConfig(url=...))`, `x402Facilitator()`, `register_exact_evm_facilitator(fac, FacilitatorWeb3Signer(...), networks="eip155:84532")`, `RouteConfig`, `PaymentOption`.

**Implementer brief:** Build the FastAPI seller with four paid routes exactly as design §4 lists them. The stall hook for `/research-slow` is **not** part of this task (M5 adds it); here `/research-slow` returns immediately. The app must import nothing from `billguard`. The facilitator client is chosen by `SELLER_FACILITATOR`: `remote` (default, `FACILITATOR_URL`, default `https://x402.org/facilitator`) or `local` (in-process SDK facilitator signed by `FACILITATOR_KEY_SEPOLIA`). `/convert` always returns a 12-page PDF regardless of the requested page count. Response shapes must match the schemas in the plan (BillGuard validates them in M2): report `{ticker, name, revenue_usd, fiscal_year, summary}`, research `{topic, summary, sources: [str, ...]}`.

- [ ] **Step 1: Write `sellers/local_facilitator.py`**

```python
"""In-process SDK facilitator (design §4, Sugg. 3). Used iff SELLER_FACILITATOR=local.

Settles on-chain with FACILITATOR_KEY_SEPOLIA, which must hold a little Base Sepolia ETH.
The buyer still needs no ETH. Imports nothing from billguard.
"""
from __future__ import annotations

import os

from x402.facilitator import x402Facilitator
from x402.mechanisms.evm.exact.register import register_exact_evm_facilitator
from x402.mechanisms.evm.signers import FacilitatorWeb3Signer

NETWORK = "eip155:84532"


def build_local_facilitator() -> x402Facilitator:
    signer = FacilitatorWeb3Signer(
        private_key=os.environ["FACILITATOR_KEY_SEPOLIA"],
        rpc_url=os.environ.get("RPC_URL_SEPOLIA", "https://sepolia.base.org"),
    )
    fac = x402Facilitator()
    register_exact_evm_facilitator(fac, signer, networks=NETWORK)
    return fac
```

- [ ] **Step 2: Write `sellers/app.py`**

```python
"""Demo x402 sellers. Official x402 middleware only. ZERO billguard imports (PRD F11, metric 1).

Routes (design §4):
  GET  /report/{ticker}          $0.50  JSON company record
  GET  /research-slow?topic=     $0.75  JSON research result (M5 adds the after-settle stall hook)
  POST /convert {doc_id, pages}  $2.00  PDF, always 12 pages
  GET  /report-premium/{ticker}  $0.75  same as /report (registry accepts $0.50 -> PRICE_MISMATCH)
"""
from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pypdf import PdfWriter
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("sellers")

NETWORK = "eip155:84532"
PAY_TO = os.environ["SELLER_PAY_TO"]
CONVERT_PAGES = 12  # always short-delivers (scenario C)


def build_server() -> x402ResourceServer:
    mode = os.environ.get("SELLER_FACILITATOR", "remote")
    if mode == "local":
        from sellers.local_facilitator import build_local_facilitator

        server = x402ResourceServer(build_local_facilitator())
        log.info("facilitator: local in-process")
    else:
        url = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")
        server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=url)))
        log.info("facilitator: remote %s", url)
    server.register(NETWORK, ExactEvmServerScheme())
    return server


def option(price: str) -> PaymentOption:
    return PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=NETWORK, max_timeout_seconds=300)


ROUTES = {
    "GET /report/*": RouteConfig(accepts=option("$0.50"), description="Company report", mime_type="application/json"),
    "GET /report-premium/*": RouteConfig(accepts=option("$0.75"), description="Premium company report", mime_type="application/json"),
    "GET /research-slow": RouteConfig(accepts=option("$0.75"), description="Research report (slow)", mime_type="application/json"),
    "POST /convert": RouteConfig(accepts=option("$2.00"), description="Document conversion", mime_type="application/pdf"),
}

server = build_server()
app = FastAPI(title="BillGuard demo sellers")
_middleware = payment_middleware(ROUTES, server)


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    return await _middleware(request, call_next)


def company_record(ticker: str) -> dict:
    t = ticker.upper()
    seed = sum(ord(c) for c in t)
    return {
        "ticker": t,
        "name": f"{t.title()} Holdings Inc.",
        "revenue_usd": 1_000_000 * (seed % 97 + 3),
        "fiscal_year": 2025,
        "summary": f"{t} is a demo company record served by an ordinary x402 seller.",
    }


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "network": NETWORK, "pay_to": PAY_TO}


@app.get("/report/{ticker}")
async def report(ticker: str) -> JSONResponse:
    return JSONResponse(company_record(ticker))


@app.get("/report-premium/{ticker}")
async def report_premium(ticker: str) -> JSONResponse:
    rec = company_record(ticker)
    rec["tier"] = "premium"
    return JSONResponse(rec)


@app.get("/research-slow")
async def research_slow(topic: str = "untitled") -> JSONResponse:
    return JSONResponse(
        {
            "topic": topic,
            "summary": f"Three findings about {topic}, served after settlement.",
            "sources": ["https://example.com/a", "https://example.com/b"],
        }
    )


@app.post("/convert")
async def convert(body: dict) -> Response:
    writer = PdfWriter()
    for _ in range(CONVERT_PAGES):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    log.info("convert: requested pages=%s delivered=%s", body.get("pages"), CONVERT_PAGES)
    return Response(content=buf.getvalue(), media_type="application/pdf")
```

- [ ] **Step 3: Boot the seller against the remote facilitator (operator or agent with `.env`)**

Run (background): `uv run uvicorn sellers.app:app --port 8402`
Expected: startup log shows `facilitator: remote https://x402.org/facilitator` and uvicorn `Application startup complete`. If the middleware logs a capability mismatch and exits, the facilitator does not support `exact`/`eip155:84532` right now: switch to `SELLER_FACILITATOR=local` (Step 4) and keep going.

Run: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8402/report/ACME`
Expected: `402`

Run: `curl -s -D - -o /dev/null http://127.0.0.1:8402/report/ACME | grep -ci '^payment-required:'`
Expected: `1`

Run: `curl -s http://127.0.0.1:8402/health`
Expected: JSON with `"ok": true`

- [ ] **Step 4: Prove the local facilitator initializes once (design §4: do this in hour 1 even if x402.org works)**

Run: `uv run python -c "
import os; from dotenv import load_dotenv; load_dotenv('.env')
from sellers.local_facilitator import build_local_facilitator
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
s = x402ResourceServer(build_local_facilitator()); s.register('eip155:84532', ExactEvmServerScheme()); s.initialize(); print('local facilitator initialize ok')"`
Expected: `local facilitator initialize ok`

- [ ] **Step 5: Commit**

```bash
git add sellers/app.py sellers/local_facilitator.py
git commit -m "feat(sellers): FastAPI demo sellers on official x402 middleware with local facilitator fallback"
```

**Exit criteria (auto):**
- Run: `grep -rn billguard sellers/ | wc -l | tr -d ' '` → Expected: `0`
- Run: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8402/report/ACME` → Expected: `402`
- Run: `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'content-type: application/json' -d '{"doc_id":"d1","pages":20}' http://127.0.0.1:8402/convert` → Expected: `402`
- Run: `curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8402/research-slow?topic=x'` → Expected: `402`
- Run: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8402/report-premium/ACME` → Expected: `402`
- Step 4's command prints `local facilitator initialize ok`.

**Manual Sepolia check:** covered by Task 1A.3.

### Task 1A.3: `scripts/smoke_sellers.py` with `--naive-retry`, first real settlement `[W-SELLERS]`

**Maps to:** F12, metric 1, design §4 (N1 regression check, settle latency log per S2), §8.2 demo step 2, §10 hook-regression risk.
**Timebox:** 20 min including the on-chain run.
**§7 tests covered:** the hour-1 Sepolia smoke that replaces seller-in-process tests; the N1 regression check (§10).

**Files:**
- Create: `scripts/smoke_sellers.py`

**SDK calls:** `x402Client()`, `client.register("eip155:*", ExactEvmScheme(EthAccountSigner(account)))`, `client.set_spend_controls({"max_amount_per_payment": False})`, `x402HttpxClient(client, timeout=60)`, catch `PaymentError`.

**Implementer brief:** Write the plain-SDK buyer that proves the sellers are ordinary x402 endpoints. It pays `/report/ACME` and `/convert` with `x402HttpxClient` (no BillGuard code on this path), asserts a 200 with a resource and a `PAYMENT-RESPONSE` header, and prints the wall-clock latency of each paid request (this is the settle latency the review asked for in S2). The explicit `timeout=60` matters: `x402HttpxClient` is a plain `httpx.AsyncClient` and would otherwise time out at httpx's 5 s default while the facilitator is still settling (S6). `--naive-retry` hits `/research-slow` twice, catching `PaymentError` (which wraps the `ReadTimeout` once M5's stall hook exists), to show the bare SDK paying twice.

- [ ] **Step 1: Write the script**

```python
"""Pay each demo seller with the plain x402 SDK client. No BillGuard code on this path (metric 1).

Usage:
  uv run python scripts/smoke_sellers.py
  uv run python scripts/smoke_sellers.py --naive-retry   # pays /research-slow twice with the bare SDK
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from x402.client import x402Client
from x402.http.clients.httpx import PaymentError, x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
BASE = os.environ.get("SELLER_BASE_URL", "http://127.0.0.1:8402")


def make_client() -> x402Client:
    acct = Account.from_key(os.environ["BUYER_KEY_SEPOLIA"])
    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(EthAccountSigner(acct)))
    client.set_spend_controls({"max_amount_per_payment": False})  # SDK default cap is $1; /convert is $2
    print(f"buyer {acct.address}")
    return client


async def buy(http: x402HttpxClient, method: str, url: str, **kw):
    t0 = time.monotonic()
    resp = await http.request(method, url, **kw)
    dt = time.monotonic() - t0
    settled = "PAYMENT-RESPONSE" in resp.headers
    print(f"{method} {url} -> {resp.status_code} in {dt:.1f}s settle_header={'yes' if settled else 'no'} bytes={len(resp.content)}")
    return resp


async def main(naive_retry: bool) -> int:
    async with x402HttpxClient(make_client(), timeout=60) as http:
        r = await buy(http, "GET", f"{BASE}/report/ACME")
        assert r.status_code == 200, f"/report failed: {r.status_code} {r.text[:200]}"
        assert r.json()["ticker"] == "ACME"
        assert "PAYMENT-RESPONSE" in r.headers, "/report settled without PAYMENT-RESPONSE"

        r = await buy(http, "POST", f"{BASE}/convert", json={"doc_id": "d1", "pages": 20})
        assert r.status_code == 200, f"/convert failed: {r.status_code} {r.text[:200]}"
        assert r.headers["content-type"].startswith("application/pdf")
        assert "PAYMENT-RESPONSE" in r.headers, "/convert settled without PAYMENT-RESPONSE"

        if naive_retry:
            print("naive retry: the bare SDK client pays, times out on the stall, and pays again")
            for attempt in (1, 2):
                try:
                    r = await buy(http, "GET", f"{BASE}/research-slow", params={"topic": "naive"})
                    print(f"attempt {attempt}: {r.status_code}")
                except PaymentError as e:
                    print(f"attempt {attempt}: PaymentError after paying: {e}")
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--naive-retry", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().naive_retry)))
```

- [ ] **Step 2: Run the smoke against the live seller (needs funded `.env`)**

Run: `uv run python scripts/smoke_sellers.py`
Expected: two lines `... -> 200 in N.Ns settle_header=yes ...` and `SMOKE OK`. Note the `N.N` values: they are the settle latencies for S2. If `/report` takes more than 30 s, say so in the report; the registry's 40 s `timeout_s` is calibrated on this number.

- [ ] **Step 3: Commit**

```bash
git add scripts/smoke_sellers.py
git commit -m "feat(scripts): smoke_sellers pays /report and /convert with the bare SDK; --naive-retry"
```

**Exit criteria (auto):**
- Run: `grep -c 'timeout=60' scripts/smoke_sellers.py` → Expected: `1`
- Run: `grep -c 'max_amount_per_payment' scripts/smoke_sellers.py` → Expected: `1`
- Run: `grep -c 'except PaymentError' scripts/smoke_sellers.py` → Expected: `1`
- Run: `grep -rn 'import billguard\|from billguard' scripts/smoke_sellers.py | wc -l | tr -d ' '` → Expected: `0`

**Manual Sepolia check (this is the M1 checkpoint):** `SMOKE OK` printed; the seller pay_to address shows two incoming USDC transfers (0.50 and 2.00) on https://sepolia.basescan.org/address/<SELLER_PAY_TO>. Record the latencies in the M1 checkpoint note.

### Task 1B.1: `scripts/bazaar_probe.py` — list v2 USDC sellers on Base mainnet, reject Permit2, smoke two `[W-BAZAAR]`

**Maps to:** F12, metric 7, design §4 public seller, §8.1 Track B, review S6, PRD open question 1.
**Timebox:** 30 min (run in parallel with Track A). **Cut:** if the discovery endpoint needs credentials you do not have, print the listing failure and stop; M7 falls back to the written slide.
**§7 tests covered:** none (manual mainnet probe).

**Files:**
- Create: `scripts/bazaar_probe.py`

**SDK calls:** `with_bazaar(HTTPFacilitatorClient(FacilitatorConfig(url=...)))`, `client.extensions.bazaar.list_resources(ListDiscoveryResourcesParams(type="http", network="eip155:8453", limit=100))`, `x402HttpxClient(client, timeout=60)` with the spend-controls override.

**Implementer brief:** Write a CLI that lists Bazaar resources from the facilitator's discovery endpoint, keeps only x402 v2 HTTP sellers on `eip155:8453` whose `exact` option is canonical mainnet USDC (compare `asset` and `payTo` case-insensitively), **rejects any option whose `extra.assetTransferMethod == "permit2"`** (S6), filters out anything above `--max-price-minor` (default 100000 = $0.10), sorts by price, and prints a table. `--smoke N` pays the first N with the bare SDK client on the mainnet key and prints status, content-type and the first 200 chars. `--pick <url>` prints the `.env` lines M7 needs. Discovery items carry `accepts` as plain dicts; read keys defensively (`payTo` or `pay_to`, `amount` or `maxAmountRequired`).

- [ ] **Step 1: Write the script**

```python
"""List CDP Bazaar sellers, keep cheap x402 v2 USDC sellers on Base mainnet, smoke-test candidates.

Usage:
  uv run python scripts/bazaar_probe.py                      # list candidates
  uv run python scripts/bazaar_probe.py --smoke 2            # pay the two cheapest with the bare SDK
  uv run python scripts/bazaar_probe.py --pick <resource_url> # print .env lines for M7
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from x402.client import x402Client
from x402.extensions.bazaar import ListDiscoveryResourcesParams, with_bazaar
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.clients.httpx import PaymentError, x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
MAINNET = "eip155:8453"
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
FACILITATOR_URL = os.environ.get("BAZAAR_FACILITATOR_URL", os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator"))


def _opt(accept: dict) -> dict:
    """Normalize one accepts entry from the discovery listing."""
    return {
        "scheme": accept.get("scheme"),
        "network": accept.get("network"),
        "asset": str(accept.get("asset", "")).lower(),
        "pay_to": str(accept.get("payTo") or accept.get("pay_to") or "").lower(),
        "amount": int(accept.get("amount") or accept.get("maxAmountRequired") or 0),
        "extra": accept.get("extra") or {},
    }


def candidates(max_price_minor: int) -> list[dict]:
    client = with_bazaar(HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL)))
    resp = client.extensions.bazaar.list_resources(ListDiscoveryResourcesParams(type="http", network=MAINNET, limit=100))
    out: list[dict] = []
    for item in resp.items:
        if item.x402_version != 2:
            continue
        for raw in item.accepts or []:
            o = _opt(raw if isinstance(raw, dict) else raw.__dict__)
            if o["scheme"] != "exact" or o["network"] != MAINNET or o["asset"] != USDC_MAINNET:
                continue
            if str(o["extra"].get("assetTransferMethod", "")).lower() == "permit2":
                print(f"skip permit2 seller: {item.resource}", file=sys.stderr)
                continue
            if o["amount"] <= 0 or o["amount"] > max_price_minor:
                continue
            out.append({"resource": item.resource, "amount": o["amount"], "pay_to": o["pay_to"], "extra": o["extra"]})
    out.sort(key=lambda c: c["amount"])
    return out


def make_mainnet_client() -> x402Client:
    acct = Account.from_key(os.environ["BUYER_KEY_MAINNET"])
    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(EthAccountSigner(acct)))
    client.set_spend_controls({"max_amount_per_payment": False})
    print(f"mainnet buyer {acct.address}", file=sys.stderr)
    return client


async def smoke(cands: list[dict], n: int) -> None:
    async with x402HttpxClient(make_mainnet_client(), timeout=60) as http:
        for c in cands[:n]:
            try:
                r = await http.get(c["resource"])
                ct = r.headers.get("content-type", "")
                print(f"SMOKE {c['resource']} -> {r.status_code} {ct} settle_header={'PAYMENT-RESPONSE' in r.headers} body={r.text[:200]!r}")
            except PaymentError as e:
                print(f"SMOKE {c['resource']} -> PaymentError {e}")
            except Exception as e:  # noqa: BLE001
                print(f"SMOKE {c['resource']} -> {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-price-minor", type=int, default=100_000)
    ap.add_argument("--smoke", type=int, default=0)
    ap.add_argument("--pick", default=None)
    args = ap.parse_args()
    try:
        cands = candidates(args.max_price_minor)
    except Exception as e:  # noqa: BLE001
        print(f"LISTING FAILED ({type(e).__name__}): {e}\nUse the written fallback from design §8.7 for M7.")
        return 1
    print(f"{len(cands)} v2 USDC exact sellers on {MAINNET} at or below {args.max_price_minor} minor (permit2 excluded)")
    for c in cands[:25]:
        print(f"  {c['amount']:>8} minor  {c['resource']}  payTo={c['pay_to']}")
    if args.pick:
        match = next((c for c in cands if c["resource"] == args.pick), None)
        if not match:
            print("pick not in candidate list")
            return 1
        print("\n# add to .env for M7")
        print(f"BAZAAR_URL={match['resource']}")
        print(f"BAZAAR_PAY_TO={match['pay_to']}")
        print(f"BAZAAR_PRICE_MINOR={match['amount']}")
        print("BAZAAR_CONTENT_TYPE=application/json")
        print("BAZAAR_TOOL_NAME=get_public_data")
    if args.smoke:
        asyncio.run(smoke(cands, args.smoke))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the listing, then smoke two (operator; costs cents of real USDC)**

Run: `uv run python scripts/bazaar_probe.py`
Expected: a count line and up to 25 candidate rows, or `LISTING FAILED ...` (then stop; M7 uses the fallback).

Run: `uv run python scripts/bazaar_probe.py --smoke 2`
Expected: two `SMOKE <url> -> 200 ...` lines with `settle_header=True`. Keep the cheaper working one; note its URL for M7.

- [ ] **Step 3: Commit**

```bash
git add scripts/bazaar_probe.py
git commit -m "feat(scripts): bazaar_probe lists v2 USDC sellers on Base mainnet, rejects permit2, smokes candidates"
```

**Exit criteria (auto):**
- Run: `grep -c 'permit2' scripts/bazaar_probe.py` → Expected: `2` or more
- Run: `grep -c 'timeout=60' scripts/bazaar_probe.py` → Expected: `1`
- Run: `grep -c 'max_amount_per_payment' scripts/bazaar_probe.py` → Expected: `1`
- Run: `grep -c '\.lower()' scripts/bazaar_probe.py` → Expected: `4` or more (asset and payTo compared case-insensitively)
- Run: `uv run python scripts/bazaar_probe.py --help | head -1` → Expected: starts with `usage:`

**Manual mainnet check:** two candidates smoke-tested; the winner's URL, payTo and price recorded in the M1 checkpoint note (and in `.env` via `--pick` when M7 starts).

### Milestone M1 — Checkpoint and manual verification

**Checkpoint (§8.1):** one real Sepolia settlement.

Operator verification list (tick all):
- [ ] `uv run python scripts/setup_wallets.py --check` prints `CHECK OK` (buyer Sepolia USDC ≥ 4).
- [ ] Seller is up: `curl -s http://127.0.0.1:8402/health` returns `"ok": true`; unpaid `GET /report/ACME` returns 402 with a `PAYMENT-REQUIRED` header.
- [ ] `uv run python scripts/smoke_sellers.py` prints `SMOKE OK`; both paid calls show `settle_header=yes`. Write down both latencies.
- [ ] Explorer: the seller pay_to address shows the 0.50 and 2.00 USDC transfers on Sepolia basescan.
- [ ] Local facilitator `initialize()` printed `local facilitator initialize ok` (Task 1A.2 step 4), and the facilitator address holds Sepolia ETH.
- [ ] Track B: `bazaar_probe.py` printed a candidate list; two were smoke-tested; the winner is written down. If the listing failed, the fallback slide is on the M9 list.
- [ ] `grep -rn billguard sellers/` is empty.
- [ ] If the checkpoint is not met by 23:00: set `SELLER_FACILITATOR=local` in `.env`, restart the seller, rerun the smoke. If that also fails, continue to M2 (its pytest suite uses a fake seller) and retry the facilitator every 30 min.

---

## Milestone M2 — Core purchase flow `[W-CORE]`

**Timebox:** 2 h (22:20–00:20; task timeboxes sum to 120 min: 10+20+15+10+10+35+15+5). **Cut if overrun:** nothing inside the flow is cut (design §8 has no step-2 cut); the overrun is absorbed by the 03:00 rule that moves the Supabase flip to stretch. Do not skip the pytest fake-seller suite: it is what makes M3–M5 safe to build in parallel.
**Checkpoint (§8.2):** Claude Desktop buys `/report` through BillGuard and the response shows `SETTLED` + `VERIFIED` + a Sepolia transaction.

**Interfaces frozen here for M3–M5** (bodies may be stubs; signatures may not change later):
- `guard.resolve_intent(deps, service, args, request_fingerprint, purchase_intent_id, after_reconcile=False) -> IntentDecision`
- `reconcile.reconcile(deps, row, deadline) -> Purchase`
- `recovery.run_startup_recovery(deps) -> None` (async)
- `verify.run_validators(service, args, artifact) -> list[tuple[str, bool, dict]]`
- `artifacts.store_artifact(body, content_type, purchase_id, root) -> Artifact`, `artifacts.load_latest(repo, purchase_id) -> Artifact | None`
- `outcome.send_paid_request(http, method, url, body, header, timeout_s) -> Outcome`, `outcome.classify_outcome(repo, row, outcome, from_state, artifact_dir, via) -> Purchase`
- `purchase.reconcile_and_finish(deps, row, deadline) -> Purchase`, `purchase.run_from_quote(deps, row, service, args, deadline) -> Purchase`

### Task 2.1: `billguard/models.py` — states, transitions, finding codes, row types `[W-CORE]`

**Maps to:** F3 (state machine), F9 (budget states), design §3 state machine, data model, finding codes, `safe_next_actions` vocabulary.
**Timebox:** 10 min.
**SDK calls:** none (pure Python).
**§7 tests covered:** "one case per transition" (transition table test).

**Files:**
- Create: `billguard/models.py`
- Test: `tests/test_models.py`

**Implementer brief:** Encode design §3 as data: the two status enums, the transition table (compare-and-set writes consult it), the budget state set, finding codes, reject reasons, evidence kinds, the `safe_next_actions` vocabulary (which must never contain a pay/retry value), the `Purchase`/`Evidence`/`Finding` row types, and the `request_fingerprint` helper. **Plan decision:** rows carry `args` (JSON) and `reject_reason` columns in addition to design §3's list; `args` is needed to resend the identical request during reconciliation and to re-run steps 2–7 from `approve_retry.py`; `reject_reason` carries codes such as `SIGNING_FAILED` that are not finding codes. `INTENT_RECORDED -> REJECTED` is a legal transition because design §3 rejects `NOT_X402`, unsupported versions and `SERVICE_QUARANTINED` before quoting.

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
from billguard.models import (
    BUDGET_STATES,
    IN_FLIGHT,
    SAFE_NEXT_ACTION_PREFIXES,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    assert_transition,
    fingerprint,
)
import pytest


def test_fingerprint_is_canonical():
    a = fingerprint("report", {"ticker": "ACME", "x": 1})
    b = fingerprint("report", {"x": 1, "ticker": "ACME"})
    assert a == b and len(a) == 64
    assert fingerprint("report", {"ticker": "acme"}) != a


@pytest.mark.parametrize(
    "src,dst",
    [
        ("INTENT_RECORDED", "QUOTED"), ("QUOTED", "AUTHORIZED"), ("QUOTED", "REJECTED"),
        ("AUTHORIZED", "PAYLOAD_SIGNED"), ("AUTHORIZED", "REJECTED"), ("PAYLOAD_SIGNED", "SENT"),
        ("SENT", "SETTLED"), ("SENT", "SETTLEMENT_UNKNOWN"), ("SETTLEMENT_UNKNOWN", "SETTLED"),
        ("SETTLEMENT_UNKNOWN", "PAYMENT_FAILED"), ("INTENT_RECORDED", "ABANDONED"),
        ("QUOTED", "ABANDONED"), ("AUTHORIZED", "ABANDONED"), ("PAYLOAD_SIGNED", "SETTLEMENT_UNKNOWN"),
        ("INTENT_RECORDED", "REJECTED"), ("SETTLEMENT_UNKNOWN", "SETTLEMENT_UNKNOWN"),
    ],
)
def test_allowed_transitions(src, dst):
    assert_transition(PaymentStatus(src), PaymentStatus(dst))


@pytest.mark.parametrize(
    "src,dst",
    [("SETTLED", "SETTLEMENT_UNKNOWN"), ("REJECTED", "QUOTED"), ("SENT", "ABANDONED"),
     ("PAYLOAD_SIGNED", "ABANDONED"), ("PAYMENT_FAILED", "SETTLED"), ("SETTLED", "PAYMENT_FAILED")],
)
def test_forbidden_transitions(src, dst):
    with pytest.raises(ValueError):
        assert_transition(PaymentStatus(src), PaymentStatus(dst))


def test_budget_states_match_design():
    assert BUDGET_STATES == {PaymentStatus.AUTHORIZED, PaymentStatus.PAYLOAD_SIGNED, PaymentStatus.SENT,
                             PaymentStatus.SETTLEMENT_UNKNOWN, PaymentStatus.SETTLED}
    assert PaymentStatus.REJECTED not in BUDGET_STATES and PaymentStatus.ABANDONED not in BUDGET_STATES
    assert IN_FLIGHT == {PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, PaymentStatus.AUTHORIZED,
                         PaymentStatus.PAYLOAD_SIGNED, PaymentStatus.SENT}


def test_next_action_vocabulary_has_no_pay_or_retry():
    for v in SAFE_NEXT_ACTION_PREFIXES:
        assert "pay" not in v and "retry" not in v
    assert NextAction.wait_until(123).startswith("wait_until:")


def test_purchase_new_defaults():
    p = Purchase.new(principal_id="p", task_id=None, purchase_intent_id="i1", request_fingerprint="f" * 64,
                     service_id="report", args={"ticker": "ACME"}, network="eip155:84532")
    assert p.payment_status == PaymentStatus.INTENT_RECORDED
    assert p.fulfillment_status == FulfillmentStatus.PENDING
    assert p.attempt == 1 and p.history[0]["state"] == "INTENT_RECORDED"
    assert len(p.purchase_id) == 32
```

- [ ] **Step 2: Run the test and see it fail**

Run: `uv run pytest tests/test_models.py -q`
Expected: `ModuleNotFoundError: No module named 'billguard.models'`

- [ ] **Step 3: Write `billguard/models.py`**

```python
"""Core types of the purchase state machine (design §3). Money is always integer minor units."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class PaymentStatus(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    QUOTED = "QUOTED"
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    PAYLOAD_SIGNED = "PAYLOAD_SIGNED"
    SENT = "SENT"
    SETTLED = "SETTLED"
    SETTLEMENT_UNKNOWN = "SETTLEMENT_UNKNOWN"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    ABANDONED = "ABANDONED"


class FulfillmentStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"
    NO_DELIVERY = "NO_DELIVERY"


P = PaymentStatus
IN_FLIGHT = frozenset({P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT})
BUDGET_STATES = frozenset({P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN, P.SETTLED})
UNSIGNED_IN_FLIGHT = frozenset({P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED})  # recovery -> ABANDONED
SIGNED_IN_FLIGHT = frozenset({P.PAYLOAD_SIGNED, P.SENT})  # recovery -> SETTLEMENT_UNKNOWN

# Design §3 transition table. SETTLEMENT_UNKNOWN -> SETTLEMENT_UNKNOWN is the resend classification no-op.
TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    P.INTENT_RECORDED: frozenset({P.QUOTED, P.REJECTED, P.ABANDONED}),
    P.QUOTED: frozenset({P.AUTHORIZED, P.REJECTED, P.ABANDONED}),
    P.AUTHORIZED: frozenset({P.PAYLOAD_SIGNED, P.REJECTED, P.ABANDONED}),
    P.PAYLOAD_SIGNED: frozenset({P.SENT, P.SETTLEMENT_UNKNOWN}),
    P.SENT: frozenset({P.SETTLED, P.SETTLEMENT_UNKNOWN}),
    P.SETTLEMENT_UNKNOWN: frozenset({P.SETTLED, P.PAYMENT_FAILED, P.SETTLEMENT_UNKNOWN}),
    P.SETTLED: frozenset(),
    P.REJECTED: frozenset(),
    P.PAYMENT_FAILED: frozenset(),
    P.ABANDONED: frozenset(),
}


def assert_transition(expected: PaymentStatus, new: PaymentStatus) -> None:
    if new not in TRANSITIONS[expected]:
        raise ValueError(f"illegal transition {expected} -> {new}")


class FindingCode(StrEnum):
    PRICE_MISMATCH = "PRICE_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NOT_X402 = "NOT_X402"
    SERVICE_QUARANTINED = "SERVICE_QUARANTINED"
    SETTLEMENT_UNKNOWN = "SETTLEMENT_UNKNOWN"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAID_NO_DELIVERY = "PAID_NO_DELIVERY"
    FULFILLMENT_FAILED = "FULFILLMENT_FAILED"
    FULFILLMENT_UNVERIFIED = "FULFILLMENT_UNVERIFIED"
    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"


class RejectReason(StrEnum):
    NOT_X402 = "NOT_X402"
    UNSUPPORTED_X402_VERSION = "UNSUPPORTED_X402_VERSION"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SIGNING_FAILED = "SIGNING_FAILED"
    SERVICE_QUARANTINED = "SERVICE_QUARANTINED"
    POLICY_DENIED = "POLICY_DENIED"
    QUOTE_FAILED = "QUOTE_FAILED"  # unpaid request could not be made (seller down); nothing signed, no proven finding


class EvidenceKind(StrEnum):
    QUOTE = "quote"
    SELECTED_REQUIREMENT = "selected_requirement"
    AUTHORIZATION = "authorization"
    PAYMENT_PAYLOAD = "payment_payload"
    SETTLEMENT = "settlement"
    SELLER_ERROR = "seller_error"
    CHAIN_CHECK = "chain_check"
    RESEND = "resend"
    RETRY_BLOCKED = "retry_blocked"
    ARTIFACT = "artifact"
    VALIDATOR = "validator"


class NextAction:
    """safe_next_actions vocabulary (design §3). No value means pay or retry."""

    GET_PURCHASE = "get_purchase"
    RECONCILE = "reconcile_purchase"
    HUMAN_REVIEW = "human_review"
    REPORT_TO_SELLER = "report_to_seller"
    USE_ARTIFACT = "use_artifact"

    @staticmethod
    def wait_until(valid_before: int) -> str:
        return f"wait_until:{int(valid_before)}"


SAFE_NEXT_ACTION_PREFIXES = (
    NextAction.GET_PURCHASE,
    NextAction.RECONCILE,
    "wait_until:",
    NextAction.HUMAN_REVIEW,
    NextAction.REPORT_TO_SELLER,
    NextAction.USE_ARTIFACT,
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(service_id: str, args: dict[str, Any]) -> str:
    return hashlib.sha256((service_id + canonical_json(args)).encode()).hexdigest()


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def history_entry(state: str, **detail: Any) -> dict[str, Any]:
    return {"at": iso_now(), "state": state, **detail}


@dataclass
class Purchase:
    purchase_id: str
    principal_id: str
    task_id: str | None
    purchase_intent_id: str
    request_fingerprint: str
    attempt: int
    supersedes_purchase_id: str | None
    approved_by: str | None
    service_id: str
    args: dict[str, Any]
    quoted_amount_minor: int | None
    authorized_amount_minor: int | None
    asset: str | None
    network: str
    pay_to: str | None
    payment_status: PaymentStatus
    fulfillment_status: FulfillmentStatus
    settlement_tx: str | None
    valid_before: int | None
    artifact_hash: str | None
    reject_reason: str | None
    history: list[dict[str, Any]]
    created_at: float
    updated_at: float

    @classmethod
    def new(
        cls,
        *,
        principal_id: str,
        task_id: str | None,
        purchase_intent_id: str,
        request_fingerprint: str,
        service_id: str,
        args: dict[str, Any],
        network: str,
        attempt: int = 1,
        supersedes_purchase_id: str | None = None,
        approved_by: str | None = None,
    ) -> "Purchase":
        t = now()
        return cls(
            purchase_id=new_id(), principal_id=principal_id, task_id=task_id,
            purchase_intent_id=purchase_intent_id, request_fingerprint=request_fingerprint,
            attempt=attempt, supersedes_purchase_id=supersedes_purchase_id, approved_by=approved_by,
            service_id=service_id, args=dict(args), quoted_amount_minor=None, authorized_amount_minor=None,
            asset=None, network=network, pay_to=None, payment_status=P.INTENT_RECORDED,
            fulfillment_status=FulfillmentStatus.PENDING, settlement_tx=None, valid_before=None,
            artifact_hash=None, reject_reason=None,
            history=[history_entry("INTENT_RECORDED", attempt=attempt)], created_at=t, updated_at=t,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["payment_status"] = str(self.payment_status)
        d["fulfillment_status"] = str(self.fulfillment_status)
        return d


@dataclass
class Evidence:
    evidence_id: str
    purchase_id: str
    kind: str
    payload: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    finding_id: str
    purchase_id: str
    code: str
    severity: str  # "proven" | "suspected"
    summary: str
    evidence_ids: list[str]
    safe_next_actions: list[str]
    created_at: float
    resolved_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: Run the test and see it pass**

Run: `uv run pytest tests/test_models.py -q`
Expected: all pass (`... passed`).

- [ ] **Step 5: Commit**

```bash
git add billguard/models.py tests/test_models.py
git commit -m "feat(core): purchase state machine types, transitions, finding codes"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_models.py -q` → Expected: `passed`, 0 failed
- Run: `uv run python -c "from billguard.models import FindingCode; print(sorted(c.value for c in FindingCode))"` → Expected: `['BUDGET_EXCEEDED', 'DUPLICATE_SUSPECTED', 'FULFILLMENT_FAILED', 'FULFILLMENT_UNVERIFIED', 'NOT_X402', 'PAID_NO_DELIVERY', 'PAYMENT_FAILED', 'PRICE_MISMATCH', 'SERVICE_QUARANTINED', 'SETTLEMENT_UNKNOWN']`

**Manual Sepolia check:** none.

### Task 2.2: `Repository` protocol and SQLite backend `[W-CORE]`

**Maps to:** F3 (compare-and-set), F9 (spent), F13 (Repository protocol), non-functional WAL + busy timeout (S7), design §2 Repository list, §3 data model, §8 reset semantics.
**Timebox:** 20 min.
**SDK calls:** none (sqlite3 stdlib).
**§7 tests covered:** "compare-and-set loss" (store level); budget sum; reset.

**Files:**
- Create: `billguard/store/base.py`, `billguard/store/sqlite.py`
- Modify: `billguard/store/__init__.py`
- Test: `tests/test_store_sqlite.py`

**Implementer brief:** Implement the design's `Repository` protocol over SQLite. Every status write is `update_status(purchase_id, expected, new, history_entry=None, **fields)`: one `UPDATE ... WHERE purchase_id=? AND payment_status=?` that returns `True` only when exactly one row changed, appends the history entry in the same statement with SQLite's `json_insert`, and refuses illegal transitions via `models.assert_transition`. Open the DB in WAL mode with `busy_timeout=5000` and `check_same_thread=False` (Claude Desktop can spawn the server twice). JSON columns are TEXT. `reset(principal_id)` deletes that principal's purchases, evidence and findings and returns the counts.

- [ ] **Step 1: Write `billguard/store/base.py`**

```python
"""Repository protocol (design §2). Two backends: sqlite (default) and supabase (M6)."""
from __future__ import annotations

from typing import Any, Protocol

from billguard.models import Evidence, Finding, FulfillmentStatus, PaymentStatus, Purchase


class DuplicateKeyError(Exception):
    """(principal_id, purchase_intent_id, request_fingerprint, attempt) already exists."""


class Repository(Protocol):
    # purchases
    def create_purchase(self, row: Purchase) -> Purchase: ...
    def get_purchase(self, purchase_id: str) -> Purchase | None: ...
    def find_by_key(self, principal_id: str, purchase_intent_id: str, request_fingerprint: str) -> list[Purchase]: ...
    def find_recent_by_fingerprint(self, principal_id: str, request_fingerprint: str, since_ts: float) -> list[Purchase]: ...
    def update_status(self, purchase_id: str, expected: PaymentStatus, new: PaymentStatus,
                      history_entry: dict[str, Any] | None = None, **fields: Any) -> bool: ...
    def set_fulfillment(self, purchase_id: str, status: FulfillmentStatus,
                        history_entry: dict[str, Any] | None = None, **fields: Any) -> None: ...
    def append_history(self, purchase_id: str, entry: dict[str, Any]) -> None: ...
    def list_stuck(self, older_than_s: float) -> list[Purchase]: ...
    def spent_minor(self, principal_id: str) -> int: ...
    # evidence
    def add_evidence(self, purchase_id: str, kind: str, payload: dict[str, Any]) -> str: ...
    def list_evidence(self, purchase_id: str) -> list[Evidence]: ...
    # findings
    def add_finding(self, purchase_id: str, code: str, severity: str, summary: str,
                    evidence_ids: list[str], safe_next_actions: list[str]) -> str: ...
    def findings_for_purchase(self, purchase_id: str) -> list[Finding]: ...
    def has_finding(self, purchase_id: str, code: str) -> bool: ...
    def has_unresolved_service_finding(self, principal_id: str, service_id: str, code: str) -> bool: ...
    def resolve_findings(self, purchase_id: str, code: str) -> int: ...
    def resolve_service_findings(self, principal_id: str, service_id: str, code: str) -> int: ...
    def list_findings(self, principal_id: str, unresolved_only: bool = True) -> list[dict[str, Any]]: ...
    # admin
    def reset(self, principal_id: str) -> dict[str, int]: ...
```

- [ ] **Step 2: Write the failing tests**

`tests/test_store_sqlite.py`:
```python
import time

import pytest

from billguard.models import FulfillmentStatus, PaymentStatus, Purchase, history_entry
from billguard.store.base import DuplicateKeyError
from billguard.store.sqlite import SqliteRepository


@pytest.fixture
def repo(tmp_path):
    return SqliteRepository(tmp_path / "t.db")


def mk(intent="i1", fp="f" * 64, attempt=1, service="report"):
    return Purchase.new(principal_id="p", task_id="t", purchase_intent_id=intent, request_fingerprint=fp,
                        service_id=service, args={"ticker": "ACME"}, network="eip155:84532", attempt=attempt)


def test_wal_and_busy_timeout(repo):
    assert repo.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert repo.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_create_get_and_unique_key(repo):
    row = repo.create_purchase(mk())
    got = repo.get_purchase(row.purchase_id)
    assert got.purchase_id == row.purchase_id and got.args == {"ticker": "ACME"}
    with pytest.raises(DuplicateKeyError):
        repo.create_purchase(mk())
    repo.create_purchase(mk(attempt=2))  # a new attempt is a new key
    assert [r.attempt for r in repo.find_by_key("p", "i1", "f" * 64)] == [1, 2]


def test_update_status_is_compare_and_set(repo):
    row = repo.create_purchase(mk())
    assert repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED,
                              history_entry=history_entry("QUOTED", amount=500000), quoted_amount_minor=500000)
    assert not repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED)  # lost race
    got = repo.get_purchase(row.purchase_id)
    assert got.payment_status == PaymentStatus.QUOTED and got.quoted_amount_minor == 500000
    assert [h["state"] for h in got.history] == ["INTENT_RECORDED", "QUOTED"]
    with pytest.raises(ValueError):
        repo.update_status(row.purchase_id, PaymentStatus.QUOTED, PaymentStatus.SETTLED)  # illegal transition


def test_spent_counts_only_budget_states(repo):
    a = repo.create_purchase(mk(intent="a"))
    b = repo.create_purchase(mk(intent="b"))
    c = repo.create_purchase(mk(intent="c"))
    for r in (a, b, c):
        repo.update_status(r.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED, quoted_amount_minor=500000)
        repo.update_status(r.purchase_id, PaymentStatus.QUOTED, PaymentStatus.AUTHORIZED, authorized_amount_minor=500000)
    repo.update_status(b.purchase_id, PaymentStatus.AUTHORIZED, PaymentStatus.REJECTED, reject_reason="SIGNING_FAILED")
    repo.update_status(c.purchase_id, PaymentStatus.AUTHORIZED, PaymentStatus.ABANDONED)
    assert repo.spent_minor("p") == 500000


def test_evidence_and_findings(repo):
    row = repo.create_purchase(mk())
    e1 = repo.add_evidence(row.purchase_id, "quote", {"amount": "500000"})
    f1 = repo.add_finding(row.purchase_id, "PRICE_MISMATCH", "proven", "too expensive", [e1], ["get_purchase"])
    assert [e.kind for e in repo.list_evidence(row.purchase_id)] == ["quote"]
    assert repo.has_finding(row.purchase_id, "PRICE_MISMATCH")
    assert repo.has_unresolved_service_finding("p", "report", "PRICE_MISMATCH")
    listed = repo.list_findings("p", unresolved_only=True)
    assert listed[0]["finding_id"] == f1 and listed[0]["service_id"] == "report"
    assert repo.resolve_findings(row.purchase_id, "PRICE_MISMATCH") == 1
    assert not repo.has_unresolved_service_finding("p", "report", "PRICE_MISMATCH")
    assert repo.list_findings("p", unresolved_only=True) == []
    assert len(repo.list_findings("p", unresolved_only=False)) == 1


def test_list_stuck_and_recent_fingerprint(repo):
    old = repo.create_purchase(mk(intent="old"))
    repo.conn.execute("UPDATE purchases SET updated_at = ?, created_at = ? WHERE purchase_id = ?",
                      (time.time() - 120, time.time() - 120, old.purchase_id))
    fresh = repo.create_purchase(mk(intent="fresh"))
    assert [r.purchase_id for r in repo.list_stuck(60)] == [old.purchase_id]
    recent = repo.find_recent_by_fingerprint("p", "f" * 64, since_ts=time.time() - 60)
    assert [r.purchase_id for r in recent] == [fresh.purchase_id]


def test_reset_clears_principal(repo):
    row = repo.create_purchase(mk())
    repo.add_evidence(row.purchase_id, "quote", {})
    repo.add_finding(row.purchase_id, "NOT_X402", "proven", "x", [], [])
    counts = repo.reset("p")
    assert counts == {"purchases": 1, "evidence": 1, "findings": 1}
    assert repo.get_purchase(row.purchase_id) is None


def test_set_fulfillment(repo):
    row = repo.create_purchase(mk())
    repo.set_fulfillment(row.purchase_id, FulfillmentStatus.VERIFIED, history_entry("VERIFIED"), artifact_hash="abc")
    got = repo.get_purchase(row.purchase_id)
    assert got.fulfillment_status == FulfillmentStatus.VERIFIED and got.artifact_hash == "abc"
```

- [ ] **Step 3: Run the tests and see them fail**

Run: `uv run pytest tests/test_store_sqlite.py -q`
Expected: `ModuleNotFoundError: No module named 'billguard.store.sqlite'`

- [ ] **Step 4: Write `billguard/store/sqlite.py`**

```python
"""SQLite repository (build-night default). WAL mode + busy_timeout 5000 ms (review S7)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from billguard.models import (
    BUDGET_STATES,
    IN_FLIGHT,
    Evidence,
    Finding,
    FulfillmentStatus,
    PaymentStatus,
    Purchase,
    assert_transition,
    new_id,
    now,
)
from billguard.store.base import DuplicateKeyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
  purchase_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  task_id TEXT,
  purchase_intent_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  supersedes_purchase_id TEXT,
  approved_by TEXT,
  service_id TEXT NOT NULL,
  args TEXT NOT NULL,
  quoted_amount_minor INTEGER,
  authorized_amount_minor INTEGER,
  asset TEXT,
  network TEXT NOT NULL,
  pay_to TEXT,
  payment_status TEXT NOT NULL,
  fulfillment_status TEXT NOT NULL,
  settlement_tx TEXT,
  valid_before INTEGER,
  artifact_hash TEXT,
  reject_reason TEXT,
  history TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (principal_id, purchase_intent_id, request_fingerprint, attempt)
);
CREATE INDEX IF NOT EXISTS idx_purchases_fp ON purchases (principal_id, request_fingerprint, created_at);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  purchase_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_purchase ON evidence (purchase_id, created_at);
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  purchase_id TEXT NOT NULL,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  safe_next_actions TEXT NOT NULL,
  created_at REAL NOT NULL,
  resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_findings_purchase ON findings (purchase_id);
"""

PURCHASE_COLS = (
    "purchase_id", "principal_id", "task_id", "purchase_intent_id", "request_fingerprint", "attempt",
    "supersedes_purchase_id", "approved_by", "service_id", "args", "quoted_amount_minor",
    "authorized_amount_minor", "asset", "network", "pay_to", "payment_status", "fulfillment_status",
    "settlement_tx", "valid_before", "artifact_hash", "reject_reason", "history", "created_at", "updated_at",
)
JSON_COLS = {"args", "history"}
PROTECTED_COLS = {"purchase_id", "payment_status", "history", "principal_id"}


def _enc(col: str, v: Any) -> Any:
    if col in JSON_COLS or isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, (PaymentStatus, FulfillmentStatus)):
        return str(v)
    return v


def _row(r: sqlite3.Row) -> Purchase:
    d = {k: r[k] for k in PURCHASE_COLS}
    d["args"] = json.loads(d["args"])
    d["history"] = json.loads(d["history"])
    d["payment_status"] = PaymentStatus(d["payment_status"])
    d["fulfillment_status"] = FulfillmentStatus(d["fulfillment_status"])
    return Purchase(**d)


def _finding(r: sqlite3.Row) -> Finding:
    return Finding(
        finding_id=r["finding_id"], purchase_id=r["purchase_id"], code=r["code"], severity=r["severity"],
        summary=r["summary"], evidence_ids=json.loads(r["evidence_ids"]),
        safe_next_actions=json.loads(r["safe_next_actions"]), created_at=r["created_at"], resolved_at=r["resolved_at"],
    )


class SqliteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # isolation_level=None -> autocommit; every UPDATE is its own atomic transaction.
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    # ---- purchases -------------------------------------------------------
    def create_purchase(self, row: Purchase) -> Purchase:
        d = row.to_dict()
        cols = ", ".join(PURCHASE_COLS)
        marks = ", ".join("?" for _ in PURCHASE_COLS)
        try:
            self.conn.execute(f"INSERT INTO purchases ({cols}) VALUES ({marks})",
                              [_enc(c, d[c]) for c in PURCHASE_COLS])
        except sqlite3.IntegrityError as e:
            raise DuplicateKeyError(str(e)) from e
        return self.get_purchase(row.purchase_id)  # type: ignore[return-value]

    def get_purchase(self, purchase_id: str) -> Purchase | None:
        r = self.conn.execute("SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,)).fetchone()
        return _row(r) if r else None

    def find_by_key(self, principal_id: str, purchase_intent_id: str, request_fingerprint: str) -> list[Purchase]:
        rows = self.conn.execute(
            "SELECT * FROM purchases WHERE principal_id = ? AND purchase_intent_id = ? AND request_fingerprint = ? ORDER BY attempt",
            (principal_id, purchase_intent_id, request_fingerprint),
        ).fetchall()
        return [_row(r) for r in rows]

    def find_recent_by_fingerprint(self, principal_id: str, request_fingerprint: str, since_ts: float) -> list[Purchase]:
        rows = self.conn.execute(
            "SELECT * FROM purchases WHERE principal_id = ? AND request_fingerprint = ? AND created_at >= ? ORDER BY created_at DESC",
            (principal_id, request_fingerprint, since_ts),
        ).fetchall()
        return [_row(r) for r in rows]

    def update_status(self, purchase_id: str, expected: PaymentStatus, new: PaymentStatus,
                      history_entry: dict[str, Any] | None = None, **fields: Any) -> bool:
        expected, new = PaymentStatus(expected), PaymentStatus(new)
        assert_transition(expected, new)
        bad = [k for k in fields if k not in PURCHASE_COLS or k in PROTECTED_COLS]
        if bad:
            raise ValueError(f"cannot set columns {bad} through update_status")
        sets = ["payment_status = ?", "updated_at = ?"]
        params: list[Any] = [str(new), now()]
        if history_entry is not None:
            sets.append("history = json_insert(history, '$[#]', json(?))")
            params.append(json.dumps(history_entry))
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            params.append(_enc(k, v))
        params += [purchase_id, str(expected)]
        cur = self.conn.execute(
            f"UPDATE purchases SET {', '.join(sets)} WHERE purchase_id = ? AND payment_status = ?", params
        )
        return cur.rowcount == 1

    def set_fulfillment(self, purchase_id: str, status: FulfillmentStatus,
                        history_entry: dict[str, Any] | None = None, **fields: Any) -> None:
        bad = [k for k in fields if k not in PURCHASE_COLS or k in PROTECTED_COLS or k == "fulfillment_status"]
        if bad:
            raise ValueError(f"cannot set columns {bad} through set_fulfillment")
        sets = ["fulfillment_status = ?", "updated_at = ?"]
        params: list[Any] = [str(FulfillmentStatus(status)), now()]
        if history_entry is not None:
            sets.append("history = json_insert(history, '$[#]', json(?))")
            params.append(json.dumps(history_entry))
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            params.append(_enc(k, v))
        params.append(purchase_id)
        self.conn.execute(f"UPDATE purchases SET {', '.join(sets)} WHERE purchase_id = ?", params)

    def append_history(self, purchase_id: str, entry: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE purchases SET history = json_insert(history, '$[#]', json(?)), updated_at = ? WHERE purchase_id = ?",
            (json.dumps(entry), now(), purchase_id),
        )

    def list_stuck(self, older_than_s: float) -> list[Purchase]:
        marks = ", ".join("?" for _ in IN_FLIGHT)
        rows = self.conn.execute(
            f"SELECT * FROM purchases WHERE payment_status IN ({marks}) AND updated_at < ? ORDER BY updated_at",
            [str(s) for s in IN_FLIGHT] + [now() - older_than_s],
        ).fetchall()
        return [_row(r) for r in rows]

    def spent_minor(self, principal_id: str) -> int:
        marks = ", ".join("?" for _ in BUDGET_STATES)
        r = self.conn.execute(
            f"SELECT COALESCE(SUM(authorized_amount_minor), 0) FROM purchases WHERE principal_id = ? AND payment_status IN ({marks})",
            [principal_id] + [str(s) for s in BUDGET_STATES],
        ).fetchone()
        return int(r[0])

    # ---- evidence --------------------------------------------------------
    def add_evidence(self, purchase_id: str, kind: str, payload: dict[str, Any]) -> str:
        eid = new_id()
        self.conn.execute(
            "INSERT INTO evidence (evidence_id, purchase_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (eid, purchase_id, str(kind), json.dumps(payload, default=str), now()),
        )
        return eid

    def list_evidence(self, purchase_id: str) -> list[Evidence]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE purchase_id = ? ORDER BY created_at, rowid", (purchase_id,)
        ).fetchall()
        return [Evidence(r["evidence_id"], r["purchase_id"], r["kind"], json.loads(r["payload"]), r["created_at"]) for r in rows]

    # ---- findings --------------------------------------------------------
    def add_finding(self, purchase_id: str, code: str, severity: str, summary: str,
                    evidence_ids: list[str], safe_next_actions: list[str]) -> str:
        fid = new_id()
        self.conn.execute(
            "INSERT INTO findings (finding_id, purchase_id, code, severity, summary, evidence_ids, safe_next_actions, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, purchase_id, str(code), severity, summary, json.dumps(list(evidence_ids)), json.dumps(list(safe_next_actions)), now()),
        )
        return fid

    def findings_for_purchase(self, purchase_id: str) -> list[Finding]:
        rows = self.conn.execute("SELECT * FROM findings WHERE purchase_id = ? ORDER BY created_at, rowid", (purchase_id,)).fetchall()
        return [_finding(r) for r in rows]

    def has_finding(self, purchase_id: str, code: str) -> bool:
        r = self.conn.execute("SELECT 1 FROM findings WHERE purchase_id = ? AND code = ? LIMIT 1", (purchase_id, str(code))).fetchone()
        return r is not None

    def has_unresolved_service_finding(self, principal_id: str, service_id: str, code: str) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM findings f JOIN purchases p ON p.purchase_id = f.purchase_id "
            "WHERE p.principal_id = ? AND p.service_id = ? AND f.code = ? AND f.resolved_at IS NULL LIMIT 1",
            (principal_id, service_id, str(code)),
        ).fetchone()
        return r is not None

    def resolve_findings(self, purchase_id: str, code: str) -> int:
        cur = self.conn.execute(
            "UPDATE findings SET resolved_at = ? WHERE purchase_id = ? AND code = ? AND resolved_at IS NULL",
            (now(), purchase_id, str(code)),
        )
        return cur.rowcount

    def resolve_service_findings(self, principal_id: str, service_id: str, code: str) -> int:
        cur = self.conn.execute(
            "UPDATE findings SET resolved_at = ? WHERE resolved_at IS NULL AND code = ? AND purchase_id IN "
            "(SELECT purchase_id FROM purchases WHERE principal_id = ? AND service_id = ?)",
            (now(), str(code), principal_id, service_id),
        )
        return cur.rowcount

    def list_findings(self, principal_id: str, unresolved_only: bool = True) -> list[dict[str, Any]]:
        where = "AND f.resolved_at IS NULL" if unresolved_only else ""
        rows = self.conn.execute(
            "SELECT f.*, p.service_id, p.payment_status, p.fulfillment_status, p.authorized_amount_minor, p.purchase_intent_id "
            f"FROM findings f JOIN purchases p ON p.purchase_id = f.purchase_id WHERE p.principal_id = ? {where} "
            "ORDER BY f.created_at, f.rowid",
            (principal_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = _finding(r).to_dict()
            d.update(service_id=r["service_id"], payment_status=r["payment_status"], fulfillment_status=r["fulfillment_status"],
                     authorized_amount_minor=r["authorized_amount_minor"], purchase_intent_id=r["purchase_intent_id"])
            out.append(d)
        return out

    # ---- admin -----------------------------------------------------------
    def reset(self, principal_id: str) -> dict[str, int]:
        ids = [r[0] for r in self.conn.execute("SELECT purchase_id FROM purchases WHERE principal_id = ?", (principal_id,)).fetchall()]
        counts = {"purchases": 0, "evidence": 0, "findings": 0}
        if not ids:
            return counts
        marks = ", ".join("?" for _ in ids)
        counts["evidence"] = self.conn.execute(f"DELETE FROM evidence WHERE purchase_id IN ({marks})", ids).rowcount
        counts["findings"] = self.conn.execute(f"DELETE FROM findings WHERE purchase_id IN ({marks})", ids).rowcount
        counts["purchases"] = self.conn.execute(f"DELETE FROM purchases WHERE purchase_id IN ({marks})", ids).rowcount
        return counts
```

- [ ] **Step 5: Write `billguard/store/__init__.py`**

```python
"""Store selection by BILLGUARD_STORE (PRD F13): sqlite (default) | supabase (M6)."""
from __future__ import annotations

import os
from pathlib import Path

from billguard.store.base import DuplicateKeyError, Repository

__all__ = ["DuplicateKeyError", "Repository", "open_repository"]


def open_repository(root: Path | None = None) -> Repository:
    kind = os.environ.get("BILLGUARD_STORE", "sqlite").lower()
    if kind == "sqlite":
        from billguard.store.sqlite import SqliteRepository

        path = os.environ.get("BILLGUARD_SQLITE_PATH", "billguard.db")
        if root is not None and not os.path.isabs(path):
            path = str(root / path)
        return SqliteRepository(path)
    if kind == "supabase":
        from billguard.store.supabase import SupabaseRepository  # M6

        return SupabaseRepository.from_env()
    raise ValueError(f"unknown BILLGUARD_STORE={kind!r} (expected sqlite|supabase)")
```

- [ ] **Step 6: Run the tests and see them pass**

Run: `uv run pytest tests/test_store_sqlite.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add billguard/store tests/test_store_sqlite.py
git commit -m "feat(store): Repository protocol and SQLite backend with compare-and-set writes"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_store_sqlite.py -q` → Expected: `passed`, 0 failed
- Run: `grep -c "AND payment_status = ?" billguard/store/sqlite.py` → Expected: `1` (the compare-and-set predicate)
- Run: `grep -c "busy_timeout=5000" billguard/store/sqlite.py` → Expected: `1`

**Manual Sepolia check:** none.

### Task 2.3: Registry, policy (mandate), wallet `[W-CORE]`

**Maps to:** F4 (mandate rules), F9 (integer minor units), design §2 registry fields incl. `timeout_s` (S2), policy fields, SDK client construction, non-functional per-service timeouts.
**Timebox:** 15 min.
**§7 tests covered:** PRICE_MISMATCH / BUDGET_EXCEEDED rules behind "each step 6 outcome branch" rejections.

**Files:**
- Create: `billguard/registry.py`, `billguard/policy.py`, `billguard/wallet.py`
- Test: `tests/test_policy.py`

**SDK calls:** `x402Client()`, `client.register("eip155:*", ExactEvmScheme(EthAccountSigner(account)))`, `client.set_spend_controls({"max_amount_per_payment": False})`, `DEFAULT_ASSETS` for canonical USDC addresses.

**Implementer brief:** Three small modules. `registry.py` is the hand-written allowlist of services (design §2 fields; `timeout_s` is 15 for `/research-slow` and 40 for everything else per S2; the Bazaar entry is added only when `BAZAAR_URL` is set, M7). `policy.py` loads `policy.yaml` and implements the mandate checks that run before signing: asset must be canonical USDC and quoted amount must not exceed `accepted_price_minor` (`PRICE_MISMATCH`), and the amount must fit the per-purchase cap and the remaining budget (`BUDGET_EXCEEDED`). `wallet.py` builds the SDK client per network from env keys. **Plan decision:** `/report-premium` is reached through `get_company_report(..., tier="premium")`, which maps to the `report_premium` registry entry; that keeps PRD F1's four paid tools.

- [ ] **Step 1: Write the failing tests**

`tests/test_policy.py`:
```python
import pytest

from billguard.models import RejectReason
from billguard.policy import MandateError, Policy, check_mandate, check_service_allowed, load_policy
from billguard.registry import SEPOLIA, USDC, load_registry


@pytest.fixture
def policy():
    return Policy(principal_id="p", total_budget_minor=5_000_000, per_purchase_cap_minor=2_500_000,
                  allowed_networks=(SEPOLIA,), allowed_services=("report", "report_premium", "convert"))


@pytest.fixture
def reg():
    return load_registry(base_url="http://seller.test")


def test_registry_timeouts_and_prices(reg):
    assert reg["research"].timeout_s == 15 and reg["report"].timeout_s == 40 and reg["convert"].timeout_s == 40
    assert reg["report"].accepted_price_minor == 500_000 and reg["report_premium"].accepted_price_minor == 500_000
    assert reg["research"].accepted_price_minor == 750_000 and reg["convert"].accepted_price_minor == 2_000_000
    assert reg["report"].asset == USDC[SEPOLIA]
    assert reg["report"].build_request({"ticker": "ACME"}) == ("GET", "http://seller.test/report/ACME", None)
    assert reg["research"].build_request({"topic": "a b"}) == ("GET", "http://seller.test/research-slow?topic=a%20b", None)
    assert reg["convert"].build_request({"doc_id": "d", "pages": 20}) == ("POST", "http://seller.test/convert", {"doc_id": "d", "pages": 20})


def test_mandate_accepts_exact_price(policy, reg):
    check_mandate(policy, reg["report"], 500_000, USDC[SEPOLIA], spent_minor=0)


def test_price_mismatch_above_accepted(policy, reg):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["report_premium"], 750_000, USDC[SEPOLIA], spent_minor=0)
    assert ei.value.code == RejectReason.PRICE_MISMATCH


def test_price_mismatch_wrong_asset(policy, reg):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["report"], 500_000, "0x0000000000000000000000000000000000000001", spent_minor=0)
    assert ei.value.code == RejectReason.PRICE_MISMATCH


def test_asset_comparison_is_case_insensitive(policy, reg):
    check_mandate(policy, reg["report"], 500_000, USDC[SEPOLIA].lower(), spent_minor=0)


def test_budget_exceeded_per_purchase_cap(reg):
    p = Policy("p", 5_000_000, 100_000, (SEPOLIA,), ("report",))
    with pytest.raises(MandateError) as ei:
        check_mandate(p, reg["report"], 500_000, USDC[SEPOLIA], spent_minor=0)
    assert ei.value.code == RejectReason.BUDGET_EXCEEDED


def test_budget_exceeded_remaining(policy, reg):
    with pytest.raises(MandateError) as ei:
        check_mandate(policy, reg["convert"], 2_000_000, USDC[SEPOLIA], spent_minor=3_250_000)  # remaining 1.75
    assert ei.value.code == RejectReason.BUDGET_EXCEEDED


def test_service_allowed(policy, reg):
    check_service_allowed(policy, reg["report"])
    with pytest.raises(MandateError) as ei:
        check_service_allowed(policy, reg["research"])
    assert ei.value.code == RejectReason.POLICY_DENIED


def test_load_policy_yaml(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("principal_id: demo\ntotal_budget_minor: 5000000\nper_purchase_cap_minor: 2500000\n"
                 "allowed_networks: ['eip155:84532']\nallowed_services: [report]\n")
    p = load_policy(f)
    assert p.principal_id == "demo" and p.total_budget_minor == 5_000_000 and p.allowed_services == ("report",)
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run pytest tests/test_policy.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Write `billguard/registry.py`**

```python
"""Allowlisted services (design §2). Hand-written; four demo services plus an optional Bazaar seller."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS

SEPOLIA = "eip155:84532"
MAINNET = "eip155:8453"
USDC: dict[str, str] = {net: DEFAULT_ASSETS[net][0]["asset"] for net in (SEPOLIA, MAINNET)}


@dataclass(frozen=True)
class Service:
    id: str
    tool_name: str
    method: str
    url_template: str
    network: str
    asset: str
    accepted_price_minor: int
    expected_content_type: str
    validators: tuple[str, ...]
    timeout_s: float

    def build_request(self, args: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        if args and "{" in self.url_template:
            url = self.url_template.format(**{k: quote(str(v), safe="") for k, v in args.items()})
        else:
            url = self.url_template
        body = dict(args) if self.method.upper() != "GET" else None
        return self.method.upper(), url, body

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["accepted_price_usdc"] = f"{self.accepted_price_minor / 1_000_000:.2f}"
        return d


def load_registry(base_url: str | None = None) -> dict[str, Service]:
    base = (base_url or os.environ.get("SELLER_BASE_URL", "http://127.0.0.1:8402")).rstrip("/")
    usdc = USDC[SEPOLIA]
    services = [
        Service("report", "get_company_report", "GET", f"{base}/report/{{ticker}}", SEPOLIA, usdc, 500_000,
                "application/json", ("content_type", "non_empty", "json_schema:company_report", "ticker_matches"), 40.0),
        Service("report_premium", "get_company_report", "GET", f"{base}/report-premium/{{ticker}}", SEPOLIA, usdc, 500_000,
                "application/json", ("content_type", "non_empty", "json_schema:company_report", "ticker_matches"), 40.0),
        Service("research", "get_research_report", "GET", f"{base}/research-slow?topic={{topic}}", SEPOLIA, usdc, 750_000,
                "application/json", ("content_type", "non_empty", "json_schema:research_report"), 15.0),  # S2
        Service("convert", "convert_document", "POST", f"{base}/convert", SEPOLIA, usdc, 2_000_000,
                "application/pdf", ("content_type", "non_empty", "pdf_page_count"), 40.0),
    ]
    # --- Bazaar seller (M7): registered only when the probe's env lines are present ---
    bazaar_url = os.environ.get("BAZAAR_URL")
    if bazaar_url:
        services.append(Service(
            "bazaar", os.environ.get("BAZAAR_TOOL_NAME", "get_public_data"), "GET", bazaar_url, MAINNET, USDC[MAINNET],
            int(os.environ["BAZAAR_PRICE_MINOR"]), os.environ.get("BAZAAR_CONTENT_TYPE", "application/json"),
            ("non_empty", "content_hash"), 40.0,
        ))
    return {s.id: s for s in services}
```

- [ ] **Step 4: Write `billguard/policy.py`**

```python
"""Spending mandate (design §2 policy fields, §3 step 3). Enforced before anything is signed."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from billguard.models import RejectReason
from billguard.registry import Service


@dataclass(frozen=True)
class Policy:
    principal_id: str
    total_budget_minor: int
    per_purchase_cap_minor: int
    allowed_networks: tuple[str, ...]
    allowed_services: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_policy(path: str | Path) -> Policy:
    data = yaml.safe_load(Path(path).read_text())
    return Policy(
        principal_id=str(data["principal_id"]),
        total_budget_minor=int(data["total_budget_minor"]),
        per_purchase_cap_minor=int(data["per_purchase_cap_minor"]),
        allowed_networks=tuple(data.get("allowed_networks", [])),
        allowed_services=tuple(data.get("allowed_services", [])),
    )


class MandateError(Exception):
    def __init__(self, code: RejectReason, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_service_allowed(policy: Policy, service: Service) -> None:
    if service.id not in policy.allowed_services:
        raise MandateError(RejectReason.POLICY_DENIED, f"service '{service.id}' is not in allowed_services")
    if service.network not in policy.allowed_networks:
        raise MandateError(RejectReason.POLICY_DENIED, f"network '{service.network}' is not in allowed_networks")


def check_mandate(policy: Policy, service: Service, amount_minor: int, asset: str, spent_minor: int) -> None:
    """PRICE_MISMATCH and BUDGET_EXCEEDED rules (PRD F4). Raises MandateError; returns None when allowed."""
    if asset.lower() != service.asset.lower():
        raise MandateError(RejectReason.PRICE_MISMATCH, f"asset {asset} is not canonical USDC {service.asset} on {service.network}")
    if amount_minor > service.accepted_price_minor:
        raise MandateError(RejectReason.PRICE_MISMATCH,
                           f"quoted {amount_minor} minor exceeds accepted price {service.accepted_price_minor} minor for '{service.id}'")
    if amount_minor > policy.per_purchase_cap_minor:
        raise MandateError(RejectReason.BUDGET_EXCEEDED,
                           f"quoted {amount_minor} minor exceeds per-purchase cap {policy.per_purchase_cap_minor} minor")
    remaining = policy.total_budget_minor - spent_minor
    if amount_minor > remaining:
        raise MandateError(RejectReason.BUDGET_EXCEEDED,
                           f"quoted {amount_minor} minor exceeds remaining budget {remaining} minor (spent {spent_minor})")
```

- [ ] **Step 5: Write `billguard/wallet.py`**

```python
"""Buyer signer per network from env (design §2). Private keys never leave this module."""
from __future__ import annotations

import os
from functools import lru_cache

from eth_account import Account
from eth_account.signers.local import LocalAccount
from x402.client import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

from billguard.registry import MAINNET, SEPOLIA

_KEY_ENV = {SEPOLIA: "BUYER_KEY_SEPOLIA", MAINNET: "BUYER_KEY_MAINNET"}


def buyer_account(network: str) -> LocalAccount:
    env = _KEY_ENV.get(network)
    if env is None:
        raise ValueError(f"no buyer key mapping for network {network}")
    key = os.environ.get(env)
    if not key:
        raise RuntimeError(f"{env} is not set")
    return Account.from_key(key)


def client_for_account(account: LocalAccount) -> x402Client:
    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(EthAccountSigner(account)))
    client.set_spend_controls({"max_amount_per_payment": False})  # BillGuard owns the mandate (design §2)
    return client


@lru_cache(maxsize=4)
def build_client(network: str) -> x402Client:
    return client_for_account(buyer_account(network))


def buyer_address(network: str) -> str:
    return buyer_account(network).address
```

- [ ] **Step 6: Run the tests and see them pass**

Run: `uv run pytest tests/test_policy.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add billguard/registry.py billguard/policy.py billguard/wallet.py tests/test_policy.py
git commit -m "feat(core): service registry with per-service timeouts, mandate checks, wallet client"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_policy.py -q` → Expected: `passed`, 0 failed
- Run: `uv run python -c "from billguard.registry import load_registry; r=load_registry('http://x'); print(r['research'].timeout_s, r['report'].timeout_s)"` → Expected: `15.0 40.0`
- Run: `grep -c 'max_amount_per_payment' billguard/wallet.py` → Expected: `1`

**Manual Sepolia check:** none.

### Task 2.4: Artifacts and basic validators `[W-CORE]`

**Maps to:** F8 (validators `json_schema`, `ticker_matches`, `content_type`, `non_empty`; `pdf_page_count` and `content_hash` land in M4), design §5 validators, §6 artifact response shape (never base64; PDFs as a local path).
**Timebox:** 10 min.
**SDK calls:** pypdf (M4), jsonschema for `json_schema`.
**§7 tests covered:** "each validator" (json_schema, ticker_matches, content_type, non_empty).

**Files:**
- Create: `billguard/artifacts.py`, `billguard/verify.py`
- Test: `tests/test_verify.py` (basic cases; M4 appends)

**Implementer brief:** `artifacts.py` turns a response body into an `Artifact`: JSON bodies are parsed inline; everything else is written to `<artifact_dir>/<purchase_id>.<ext>` and referenced by path. The PDF page count is a stub returning `None` here; M4 fills it. `verify.py` holds hard-coded validator functions `(service, args, artifact) -> (name, passed, detail)` and a runner that resolves validator names from the registry (`json_schema:<schema_name>` is parameterized). Unknown validator names raise `KeyError` on purpose: the caller maps any exception to `FULFILLMENT_UNVERIFIED`.

- [ ] **Step 1: Write the failing tests**

`tests/test_verify.py`:
```python
import json
from pathlib import Path

import pytest

from billguard.artifacts import Artifact, store_artifact
from billguard.registry import load_registry
from billguard.verify import run_validators


@pytest.fixture
def reg():
    return load_registry(base_url="http://seller.test")


def json_art(obj) -> Artifact:
    return store_artifact(json.dumps(obj).encode(), "application/json", "p1", Path("/tmp/unused"))


def test_store_json_artifact_inline(tmp_path):
    art = store_artifact(b'{"a": 1}', "application/json; charset=utf-8", "p1", tmp_path)
    assert art.kind == "json" and art.json == {"a": 1} and art.path is None and len(art.sha256) == 64


def test_store_binary_artifact_as_path(tmp_path):
    art = store_artifact(b"%PDF-1.4 fake", "application/pdf", "p2", tmp_path)
    assert art.kind == "pdf" and art.path == str(tmp_path / "p2.pdf") and Path(art.path).read_bytes().startswith(b"%PDF")
    assert art.evidence()["path"] == art.path and "json" in art.evidence()


def test_report_validators_pass(reg):
    art = json_art({"ticker": "ACME", "name": "Acme", "revenue_usd": 5, "fiscal_year": 2025, "summary": "s"})
    results = run_validators(reg["report"], {"ticker": "ACME"}, art)
    assert [r[0] for r in results] == ["content_type", "non_empty", "json_schema:company_report", "ticker_matches"]
    assert all(r[1] for r in results)


def test_ticker_mismatch_fails(reg):
    art = json_art({"ticker": "OTHER", "name": "x", "revenue_usd": 1, "fiscal_year": 2025, "summary": "s"})
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["ticker_matches"][1] is False and "OTHER" in results["ticker_matches"][2]["reason"]


def test_schema_failure_has_reason(reg):
    art = json_art({"ticker": "ACME"})
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["json_schema:company_report"][1] is False and "reason" in results["json_schema:company_report"][2]


def test_content_type_and_non_empty(reg, tmp_path):
    art = store_artifact(b"", "text/plain", "p3", tmp_path)
    results = dict((r[0], r) for r in run_validators(reg["report"], {"ticker": "ACME"}, art))
    assert results["content_type"][1] is False and results["non_empty"][1] is False


def test_unknown_validator_raises(reg):
    from dataclasses import replace
    svc = replace(reg["report"], validators=("does_not_exist",))
    with pytest.raises(KeyError):
        run_validators(svc, {}, json_art({}))
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run pytest tests/test_verify.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Write `billguard/artifacts.py`**

```python
"""Delivered artifacts (design §6): inline JSON, or a local path for PDFs/bytes. Never base64."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from billguard.models import EvidenceKind


@dataclass
class Artifact:
    kind: str  # "json" | "pdf" | "bytes"
    content_type: str
    sha256: str
    size: int
    json: Any | None = None
    path: str | None = None
    page_count: int | None = None

    def evidence(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_evidence(cls, payload: dict[str, Any]) -> "Artifact":
        return cls(**{k: payload.get(k) for k in ("kind", "content_type", "sha256", "size", "json", "path", "page_count")})

    def summary(self) -> dict[str, Any]:
        """What the MCP response shows: inline JSON or a path, plus hash and page count."""
        d = {"kind": self.kind, "content_type": self.content_type, "sha256": self.sha256, "size": self.size}
        if self.kind == "json":
            d["json"] = self.json
        else:
            d["path"] = self.path
            d["page_count"] = self.page_count
        return d


def pdf_page_count(path: str) -> int | None:
    """Filled in by M4 (Task 4.1). Returns None until then."""
    return None


def store_artifact(body: bytes, content_type: str, purchase_id: str, root: Path) -> Artifact:
    sha = hashlib.sha256(body).hexdigest()
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/json" or ct.endswith("+json"):
        try:
            return Artifact("json", ct, sha, len(body), json=json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ext = "pdf" if ct == "application/pdf" else "bin"
    path = root / f"{purchase_id}.{ext}"
    path.write_bytes(body)
    art = Artifact("pdf" if ext == "pdf" else "bytes", ct, sha, len(body), path=str(path))
    if art.kind == "pdf":
        art.page_count = pdf_page_count(art.path)
    return art


def load_latest(repo: Any, purchase_id: str) -> Artifact | None:
    rows = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.ARTIFACT]
    return Artifact.from_evidence(rows[-1].payload) if rows else None
```

- [ ] **Step 4: Write `billguard/verify.py`**

```python
"""Hard-coded validators (design §5). Each returns (name, passed, detail). Exceptions mean UNVERIFIED."""
from __future__ import annotations

from typing import Any, Callable

import jsonschema

from billguard.artifacts import Artifact
from billguard.registry import Service

ValidatorResult = tuple[str, bool, dict[str, Any]]
Validator = Callable[[Service, dict[str, Any], Artifact], ValidatorResult]

SCHEMAS: dict[str, dict[str, Any]] = {
    "company_report": {
        "type": "object",
        "required": ["ticker", "name", "revenue_usd", "fiscal_year"],
        "properties": {
            "ticker": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "revenue_usd": {"type": "number"},
            "fiscal_year": {"type": "integer"},
        },
    },
    "research_report": {
        "type": "object",
        "required": ["topic", "summary", "sources"],
        "properties": {
            "topic": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "sources": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        },
    },
}


def json_schema(schema_name: str) -> Validator:
    schema = SCHEMAS[schema_name]  # KeyError on purpose for unknown schema names

    def _v(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
        name = f"json_schema:{schema_name}"
        if art.kind != "json":
            return name, False, {"reason": f"artifact is {art.kind}, not json"}
        try:
            jsonschema.validate(art.json, schema)
        except jsonschema.ValidationError as e:
            return name, False, {"reason": e.message, "path": list(e.absolute_path)}
        return name, True, {"schema": schema_name}

    return _v


def ticker_matches(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    expected = str(args.get("ticker", "")).upper()
    actual = str((art.json or {}).get("ticker", "")).upper() if art.kind == "json" else ""
    ok = bool(expected) and expected == actual
    return "ticker_matches", ok, {"expected": expected, "actual": actual, **({} if ok else {"reason": f"ticker {actual!r} != requested {expected!r}"})}


def content_type(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    ok = art.content_type.startswith(service.expected_content_type)
    return "content_type", ok, {"expected": service.expected_content_type, "actual": art.content_type,
                                **({} if ok else {"reason": f"content-type {art.content_type!r} != {service.expected_content_type!r}"})}


def non_empty(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    ok = art.size > 0 and (art.kind != "json" or art.json not in (None, {}, [], ""))
    return "non_empty", ok, {"size": art.size, **({} if ok else {"reason": "empty artifact"})}


VALIDATORS: dict[str, Validator] = {
    "ticker_matches": ticker_matches,
    "content_type": content_type,
    "non_empty": non_empty,
    # "pdf_page_count" and "content_hash" are registered by M4 (Task 4.1)
}


def resolve(name: str) -> Validator:
    if name.startswith("json_schema:"):
        return json_schema(name.split(":", 1)[1])
    return VALIDATORS[name]  # KeyError -> caller records FULFILLMENT_UNVERIFIED


def run_validators(service: Service, args: dict[str, Any], artifact: Artifact) -> list[ValidatorResult]:
    return [resolve(n)(service, args, artifact) for n in service.validators]
```

- [ ] **Step 5: Run the tests and see them pass**

Run: `uv run pytest tests/test_verify.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add billguard/artifacts.py billguard/verify.py tests/test_verify.py
git commit -m "feat(core): artifact storage and basic validators"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_verify.py -q` → Expected: `passed`, 0 failed
- Run: `grep -c base64 billguard/artifacts.py` → Expected: `1` (only the docstring says "never base64")

**Manual Sepolia check:** none.

### Task 2.5: Stubs with frozen interfaces (guard, reconcile, recovery) and the response builder `[W-CORE]`

**Maps to:** design §6 response shape and `safe_next_actions` vocabulary; sets up the parallel tracks for M3 (guard, recovery) and M5 (reconcile).
**Timebox:** 10 min.
**SDK calls:** none.
**§7 tests covered:** none directly; fixes the interfaces the M3/M5 tests target.

**Files:**
- Create: `billguard/guard.py`, `billguard/reconcile.py`, `billguard/recovery.py`, `billguard/response.py`

**Implementer brief:** Create the three modules M3 and M5 will fill, with exactly the signatures below and minimal bodies, plus the response builder that turns a `Purchase` row into the design §6 MCP response. `safe_next_actions` is computed here and must only ever contain vocabulary values.

- [ ] **Step 1: Write `billguard/guard.py` (M2 stub; M3 replaces the body of `resolve_intent`)**

```python
"""Design §3 step 1: intent lookup, duplicate guard, service quarantine.

M2 STUB: an existing key returns its latest attempt unchanged; otherwise proceed with attempt 1.
M3 (Task 3.1) implements the full decision table. The signature below is frozen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from billguard.models import Purchase
from billguard.registry import Service


@dataclass
class IntentDecision:
    action: str  # "proceed" | "return"
    row: Purchase | None = None
    attempt: int = 1
    supersedes: str | None = None
    reconcile_first: bool = False  # caller reconciles `row` outside the lock, then calls again with after_reconcile=True


def resolve_intent(deps: Any, service: Service, args: dict[str, Any], request_fingerprint: str,
                   purchase_intent_id: str, after_reconcile: bool = False) -> IntentDecision:
    rows = deps.repo.find_by_key(deps.policy.principal_id, purchase_intent_id, request_fingerprint)
    if rows:
        return IntentDecision("return", row=max(rows, key=lambda r: r.attempt))
    return IntentDecision("proceed", attempt=1)
```

- [ ] **Step 2: Write `billguard/reconcile.py` (M2 stub; M5 replaces)**

```python
"""Design §5 reconciliation for SETTLEMENT_UNKNOWN rows.

M2 STUB: returns the row unchanged. M5 (Task 5.2) implements the chain poll, the single resend and the
block-timestamp PAYMENT_FAILED rule. The signature below is frozen.
"""
from __future__ import annotations

from typing import Any

from billguard.models import Purchase


async def reconcile(deps: Any, row: Purchase, deadline: float) -> Purchase:
    return deps.repo.get_purchase(row.purchase_id) or row
```

- [ ] **Step 3: Write `billguard/recovery.py` (M2 stub; M3 replaces)**

```python
"""Startup recovery (design §3, PRD F10). Spawned from the FastMCP lifespan after a 2 s sleep.

M2 STUB: does nothing. M3 (Task 3.2) implements the 60 s staleness rule with compare-and-set writes.
"""
from __future__ import annotations

from typing import Any


async def run_startup_recovery(deps: Any) -> None:
    return None
```

- [ ] **Step 4: Write `billguard/response.py`**

```python
"""MCP response shape (design §6) and safe_next_actions (design §3)."""
from __future__ import annotations

from typing import Any

from billguard import artifacts
from billguard.models import (
    IN_FLIGHT,
    SAFE_NEXT_ACTION_PREFIXES,
    EvidenceKind,
    Finding,
    FindingCode,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    RejectReason,
)

EXPLORER = {"eip155:84532": "https://sepolia.basescan.org", "eip155:8453": "https://basescan.org"}


def safe_next_actions(row: Purchase, findings: list[Finding]) -> list[str]:
    codes = {f.code for f in findings if f.resolved_at is None}
    if row.payment_status == PaymentStatus.REJECTED and row.reject_reason == RejectReason.SERVICE_QUARANTINED:
        acts = [NextAction.HUMAN_REVIEW]
    elif codes & {FindingCode.PAID_NO_DELIVERY, FindingCode.FULFILLMENT_FAILED, FindingCode.DUPLICATE_SUSPECTED}:
        acts = [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER]  # design §3: these three always get this pair
        if row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
            acts.append(NextAction.RECONCILE)  # reconcile_purchase is allowed only while unknown
    elif row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
        acts = [NextAction.RECONCILE, NextAction.GET_PURCHASE]
        if row.valid_before:
            acts.append(NextAction.wait_until(row.valid_before))
    elif row.payment_status == PaymentStatus.SETTLED and row.fulfillment_status == FulfillmentStatus.VERIFIED:
        acts = [NextAction.USE_ARTIFACT]
    elif row.payment_status == PaymentStatus.SETTLED and row.fulfillment_status == FulfillmentStatus.UNVERIFIED:
        acts = [NextAction.HUMAN_REVIEW]
    else:
        acts = [NextAction.GET_PURCHASE]
    for a in acts:  # belt and braces: the vocabulary never contains pay/retry
        assert a.startswith(SAFE_NEXT_ACTION_PREFIXES), a
    return acts


def explorer_url(row: Purchase) -> str | None:
    base = EXPLORER.get(row.network)
    if not base:
        return None
    if row.settlement_tx:
        return f"{base}/tx/{row.settlement_tx}"
    if row.pay_to and row.payment_status == PaymentStatus.SETTLED:
        return f"{base}/address/{row.pay_to}#tokentxns"
    return None


def build_response(deps: Any, row: Purchase) -> dict[str, Any]:
    repo = deps.repo
    findings = repo.findings_for_purchase(row.purchase_id)
    evidence = repo.list_evidence(row.purchase_id)
    verification = [e.payload for e in evidence if e.kind == EvidenceKind.VALIDATOR]
    art = artifacts.load_latest(repo, row.purchase_id)
    status = "IN_PROGRESS" if row.payment_status in IN_FLIGHT else str(row.payment_status)
    settled = row.payment_status == PaymentStatus.SETTLED
    return {
        "purchase_id": row.purchase_id,
        "purchase_intent_id": row.purchase_intent_id,
        "service_id": row.service_id,
        "attempt": row.attempt,
        "supersedes_purchase_id": row.supersedes_purchase_id,
        "payment_status": status,
        "reject_reason": row.reject_reason,
        "history": row.history,
        "settled_amount": row.authorized_amount_minor if settled else None,
        "settled_amount_usdc": f"{row.authorized_amount_minor / 1_000_000:.6f}" if settled and row.authorized_amount_minor is not None else None,
        "currency": "USDC",
        "network": row.network,
        "settlement_tx": row.settlement_tx,
        "explorer_url": explorer_url(row),
        "valid_before": row.valid_before if row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN else None,
        "fulfillment_status": str(row.fulfillment_status),
        "verification": verification,
        "findings": [f.to_dict() for f in findings],
        "artifact": art.summary() if art else None,
        "safe_next_actions": safe_next_actions(row, findings),
    }
```

- [ ] **Step 5: Smoke the imports and commit**

Run: `uv run python -c "import billguard.guard, billguard.reconcile, billguard.recovery, billguard.response; print('ok')"`
Expected: `ok`

```bash
git add billguard/guard.py billguard/reconcile.py billguard/recovery.py billguard/response.py
git commit -m "feat(core): frozen interfaces for guard/reconcile/recovery and the MCP response builder"
```

**Exit criteria (auto):**
- Run: `uv run python -c "import inspect, billguard.guard as g, billguard.reconcile as r, billguard.recovery as v; print(list(inspect.signature(g.resolve_intent).parameters), list(inspect.signature(r.reconcile).parameters), list(inspect.signature(v.run_startup_recovery).parameters))"` → Expected: `['deps', 'service', 'args', 'request_fingerprint', 'purchase_intent_id', 'after_reconcile'] ['deps', 'row', 'deadline'] ['deps']`
- Run: `uv run pytest -q` → Expected: all pass

**Manual Sepolia check:** none.

### Task 2.6: Outcome classification and the purchase state machine, with a fake-seller test suite `[W-CORE]`

**Maps to:** F3 (state machine, persisted before each network call, signed header stored before send), F4 (rejections before signing), F5 (only 2xx + success=true is SETTLED; everything else reconciled), F6 (45 s wall-clock budget hook), F8 (findings on verify), F9 (budget), design §3 steps 1–7, §7 tests "one case per transition incl. compare-and-set loss", "each step 6 outcome branch".
**Timebox:** 35 min.
**§7 tests covered:** "one case per transition" (flow level, incl. compare-and-set loss), "each step 6 outcome branch".

**Files:**
- Create: `billguard/outcome.py`, `billguard/purchase.py`
- Modify: `tests/conftest.py` (replace the placeholder)
- Test: `tests/test_purchase_flow.py`

**SDK calls:** `detect_payment_required_version(headers, body)`, `decode_payment_required_header(h)`, `PaymentRequired.model_copy(update={"accepts": [selected]})`, `await client.create_payment_payload(pr)`, `payload.accepted`, `payload.payload["authorization"]`, `encode_payment_signature_header(payload)`, `decode_payment_response_header(h)`, header constants `PAYMENT_REQUIRED_HEADER`, `PAYMENT_SIGNATURE_HEADER`, `PAYMENT_RESPONSE_HEADER`. Tests build a fake v2 seller with `encode_payment_required_header`, `encode_payment_response_header`, `PaymentRequired`, `PaymentRequirements`, `ResourceInfo`, `SettleResponse`.

**Implementer brief:** This is the heart of BillGuard. Implement design §3 steps 2–7 in `run_from_quote` and step 1's orchestration in `purchase` (the decision itself lives in `guard.resolve_intent`, stubbed now). Rules that reviewers will check line by line: every state write is a compare-and-set through `update_status` and happens **before** the next network call; the signed `PAYMENT-SIGNATURE` header is stored as `payment_payload` evidence and the row is `PAYLOAD_SIGNED` then `SENT` before the paid request leaves; the process-wide `asyncio.Lock` covers lookup + row creation and, separately, the budget read + `AUTHORIZED` write, and is **never held during network I/O**; `create_payment_payload` is awaited with `accepts` narrowed to the selected option and the result is asserted to match; only a 2xx with a decodable `PAYMENT-RESPONSE` of `success=true` becomes `SETTLED`, every other post-send outcome becomes `SETTLEMENT_UNKNOWN` and is handed to `reconcile_and_finish` in the same call; a settled row with no artifact gets `PAID_NO_DELIVERY`; validator failures create one `FULFILLMENT_FAILED` finding each; a validator exception yields `FULFILLMENT_UNVERIFIED`. `outcome.py` is shared with M5's reconcile resend, so keep `classify_outcome` free of any reconcile logic.

- [ ] **Step 1: Replace `tests/conftest.py` with the fake seller and fixtures**

```python
"""Shared fixtures: a fake x402 v2 seller over httpx.MockTransport, a temp SQLite repo, and Deps."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from eth_account import Account
from pypdf import PdfWriter
from x402.http.constants import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import encode_payment_required_header, encode_payment_response_header
from x402.schemas import PaymentRequired, PaymentRequirements, SettleResponse
from x402.schemas.payments import ResourceInfo

from billguard.policy import Policy
from billguard.purchase import Deps
from billguard.registry import SEPOLIA, USDC, load_registry
from billguard.store.sqlite import SqliteRepository
from billguard.wallet import client_for_account

PAY_TO = "0x000000000000000000000000000000000000dEaD"
PRICES = {"/report/": 500_000, "/report-premium/": 750_000, "/research-slow": 750_000, "/convert": 2_000_000}


def pdf_bytes(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class FakeSeller:
    """Looks like the official middleware from the buyer's side.

    mode: ok | timeout | settle_failed | http_502 | no_header | bad_header | plain200 | v1 | nonce_used
    """

    def __init__(self, mode: str = "ok", asset: str = USDC[SEPOLIA], network: str = SEPOLIA,
                 price_override: int | None = None, tx: str = "0x" + "ab" * 32) -> None:
        self.mode = mode
        self.asset = asset
        self.network = network
        self.price_override = price_override
        self.tx = tx
        self.unpaid: list[httpx.Request] = []
        self.paid: list[httpx.Request] = []
        self.paid_modes: list[str] = []  # optional per-paid-request override queue, consumed left to right (M5 resend tests)
        self.on_unpaid: Any = None  # optional callback(request) before answering the unpaid request (CAS tests)

    def price_for(self, path: str) -> int:
        if self.price_override is not None:
            return self.price_override
        for prefix, p in PRICES.items():
            if path.startswith(prefix):
                return p
        return 100_000

    def _requirements(self, request: httpx.Request) -> PaymentRequirements:
        return PaymentRequirements(scheme="exact", network=self.network, asset=self.asset,
                                   amount=str(self.price_for(request.url.path)), pay_to=PAY_TO, max_timeout_seconds=300)

    def payment_required(self, request: httpx.Request, error: str = "payment required") -> httpx.Response:
        pr = PaymentRequired(x402_version=2, error=error, resource=ResourceInfo(url=str(request.url)),
                             accepts=[self._requirements(request)])
        return httpx.Response(402, headers={PAYMENT_REQUIRED_HEADER: encode_payment_required_header(pr)}, json={})

    def body_for(self, request: httpx.Request) -> tuple[bytes, str]:
        path = request.url.path
        if path.startswith("/report"):
            t = path.rsplit("/", 1)[-1].upper()
            return json.dumps({"ticker": t, "name": f"{t} Inc", "revenue_usd": 42.0, "fiscal_year": 2025, "summary": "s"}).encode(), "application/json"
        if path.startswith("/research-slow"):
            return json.dumps({"topic": request.url.params.get("topic", ""), "summary": "s", "sources": ["https://a"]}).encode(), "application/json"
        if path.startswith("/convert"):
            return pdf_bytes(12), "application/pdf"
        return b'{"ok": true}', "application/json"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "plain200":
            return httpx.Response(200, json={"free": True})
        if self.mode == "v1":
            return httpx.Response(402, json={"x402Version": 1, "accepts": [], "error": "payment required"})
        sig = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        if not sig:
            self.unpaid.append(request)
            if self.on_unpaid:
                self.on_unpaid(request)
            return self.payment_required(request)
        self.paid.append(request)
        mode = self.paid_modes.pop(0) if self.paid_modes else self.mode
        body, ct = self.body_for(request)
        settle_ok = encode_payment_response_header(SettleResponse(
            success=True, transaction=self.tx, network=self.network, payer="0x" + "11" * 20,
            amount=str(self.price_for(request.url.path))))
        if mode == "ok":
            return httpx.Response(200, content=body, headers={"content-type": ct, PAYMENT_RESPONSE_HEADER: settle_ok})
        if mode == "timeout":
            raise httpx.ReadTimeout("stalled after settle", request=request)
        if mode == "settle_failed":
            bad = encode_payment_response_header(SettleResponse(success=False, error_reason="insufficient_funds",
                                                                transaction="", network=self.network))
            return httpx.Response(402, json={}, headers={PAYMENT_RESPONSE_HEADER: bad})
        if mode == "http_502":
            return httpx.Response(502, json={"error": "facilitator unreachable"})
        if mode == "no_header":
            return httpx.Response(200, content=body, headers={"content-type": ct})
        if mode == "bad_header":
            return httpx.Response(200, content=body, headers={"content-type": ct, PAYMENT_RESPONSE_HEADER: "not-base64!!"})
        if mode == "nonce_used":
            return self.payment_required(request, error="invalid_exact_evm_nonce_already_used")
        raise AssertionError(f"unknown mode {mode}")


async def no_sleep(_s: float) -> None:
    return None


async def never_used(_network: str, _from_addr: str, _nonce: str):
    """Hermetic default chain reader: authorization never seen on-chain, block time 0 (so never 'expired').
    Duck-typed like billguard.chain.ChainState (M5) so this file needs no import that does not exist yet."""
    return SimpleNamespace(used=False, block_number=0, block_timestamp=0)


@pytest.fixture
def seller() -> FakeSeller:
    return FakeSeller()


@pytest.fixture
def account():
    return Account.create()


@pytest.fixture
def policy() -> Policy:
    return Policy(principal_id="test", total_budget_minor=5_000_000, per_purchase_cap_minor=2_500_000,
                  allowed_networks=(SEPOLIA, "eip155:8453"),
                  allowed_services=("report", "report_premium", "research", "convert", "bazaar"))


@pytest.fixture
def make_deps(tmp_path, account, policy):
    def _make(seller: FakeSeller, policy_override: Policy | None = None, chain_reader: Any = None) -> Deps:
        repo = SqliteRepository(tmp_path / "t.db")
        client = client_for_account(account)
        return Deps(
            repo=repo,
            policy=policy_override or policy,
            registry=load_registry(base_url="http://seller.test"),
            client_factory=lambda _net: client,
            http_factory=lambda svc: httpx.AsyncClient(transport=httpx.MockTransport(seller.handler), timeout=svc.timeout_s),
            artifact_dir=tmp_path / "artifacts",
            chain_reader=chain_reader or never_used,  # tests never touch a real RPC
            sleep=no_sleep,
        )
    return _make


@pytest.fixture
def deps(make_deps, seller) -> Deps:
    return make_deps(seller)
```

- [ ] **Step 2: Write the failing tests**

`tests/test_purchase_flow.py`:
```python
from dataclasses import replace

import pytest

from billguard.models import PaymentStatus, fingerprint
from billguard.purchase import purchase
from tests.conftest import FakeSeller


def kinds(deps, pid):
    return [e.kind for e in deps.repo.list_evidence(pid)]


def codes(resp):
    return [f["code"] for f in resp["findings"]]


async def test_happy_path_report_settled_verified(deps, seller):
    r = await purchase(deps, "report", {"ticker": "ACME"}, "intent-1", "task-1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "VERIFIED"
    assert r["settlement_tx"] == seller.tx and r["settled_amount"] == 500_000 and r["currency"] == "USDC"
    assert r["artifact"]["json"]["ticker"] == "ACME" and r["safe_next_actions"] == ["use_artifact"]
    assert [h["state"] for h in r["history"]] == [
        "INTENT_RECORDED", "QUOTED", "AUTHORIZED", "PAYLOAD_SIGNED", "SENT", "SETTLED", "VERIFIED"]
    assert kinds(deps, r["purchase_id"]) == [
        "quote", "selected_requirement", "authorization", "payment_payload", "artifact", "settlement",
        "validator", "validator", "validator", "validator"]
    assert len(seller.paid) == 1 and deps.repo.spent_minor("test") == 500_000 and r["findings"] == []
    assert r["explorer_url"].startswith("https://sepolia.basescan.org/tx/")


async def test_signed_header_is_stored_and_row_is_sent_before_the_paid_request(deps, seller):
    seen = {}
    original = seller.handler

    def spy(request):
        if request.headers.get("PAYMENT-SIGNATURE"):
            row = deps.repo.find_by_key("test", "i", fingerprint("report", {"ticker": "ACME"}))[0]
            seen["status"] = row.payment_status
            seen["headers"] = [e.payload["header"] for e in deps.repo.list_evidence(row.purchase_id) if e.kind == "payment_payload"]
            seen["sent_header"] = request.headers["PAYMENT-SIGNATURE"]
        return original(request)

    seller.handler = spy
    await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert seen["status"] == PaymentStatus.SENT
    assert seen["headers"] == [seen["sent_header"]]  # identical header persisted before send


async def test_not_x402_route_rejected(make_deps):
    seller = FakeSeller(mode="plain200")
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "NOT_X402" and codes(r) == ["NOT_X402"]
    assert "payment_payload" not in kinds(deps, r["purchase_id"]) and deps.repo.spent_minor("test") == 0


async def test_v1_seller_rejected(make_deps):
    seller = FakeSeller(mode="v1")
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "UNSUPPORTED_X402_VERSION"


async def test_price_mismatch_premium_before_signing(deps, seller):
    r = await purchase(deps, "report_premium", {"ticker": "ACME"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "PRICE_MISMATCH" and codes(r) == ["PRICE_MISMATCH"]
    k = kinds(deps, r["purchase_id"])
    assert "quote" in k and "payment_payload" not in k and seller.paid == [] and deps.repo.spent_minor("test") == 0
    assert r["findings"][0]["severity"] == "proven" and r["findings"][0]["evidence_ids"]


async def test_wrong_asset_is_price_mismatch(make_deps):
    seller = FakeSeller(asset="0x" + "22" * 20)
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "ACME"}, "i")
    assert r["reject_reason"] == "PRICE_MISMATCH" and seller.paid == []


async def test_budget_exceeded_on_remaining_budget(make_deps, seller):
    deps = make_deps(seller)
    assert (await purchase(deps, "convert", {"doc_id": "d", "pages": 20}, "i1"))["payment_status"] == "SETTLED"
    assert (await purchase(deps, "research", {"topic": "t"}, "i2"))["payment_status"] == "SETTLED"
    assert (await purchase(deps, "report", {"ticker": "A"}, "i3"))["payment_status"] == "SETTLED"
    r = await purchase(deps, "convert", {"doc_id": "d2", "pages": 20}, "i4")  # 3.25 spent, 1.75 left
    assert r["reject_reason"] == "BUDGET_EXCEEDED" and codes(r) == ["BUDGET_EXCEEDED"] and len(seller.paid) == 3


async def test_budget_exceeded_per_purchase_cap(make_deps, seller, policy):
    deps = make_deps(seller, policy_override=replace(policy, per_purchase_cap_minor=100_000))
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["reject_reason"] == "BUDGET_EXCEEDED" and seller.paid == []


async def test_policy_denied_service(make_deps, seller, policy):
    deps = make_deps(seller, policy_override=replace(policy, allowed_services=("convert",)))
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "REJECTED" and r["reject_reason"] == "POLICY_DENIED" and seller.unpaid == []


async def test_signing_failure_rejects_without_money(deps, seller):
    class Boom:
        async def create_payment_payload(self, *_a, **_k):
            raise RuntimeError("hsm down")

    deps.client_factory = lambda _n: Boom()
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["reject_reason"] == "SIGNING_FAILED" and seller.paid == []
    assert "payment_payload" not in kinds(deps, r["purchase_id"]) and deps.repo.spent_minor("test") == 0


@pytest.mark.parametrize("mode,expect_artifact", [
    ("timeout", False), ("settle_failed", False), ("http_502", False), ("no_header", True), ("bad_header", True)])
async def test_every_ambiguous_outcome_is_settlement_unknown(make_deps, mode, expect_artifact):
    seller = FakeSeller(mode=mode)
    deps = make_deps(seller)
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["valid_before"] is not None
    assert codes(r) == ["SETTLEMENT_UNKNOWN"] and r["findings"][0]["severity"] == "suspected"
    assert "reconcile_purchase" in r["safe_next_actions"] and any(a.startswith("wait_until:") for a in r["safe_next_actions"])
    assert not any("pay" in a or "retry" in a for a in r["safe_next_actions"])
    k = kinds(deps, r["purchase_id"])
    assert "payment_payload" in k and "seller_error" in k and (("artifact" in k) == expect_artifact)
    hs = [h["state"] for h in r["history"]]
    assert hs[hs.index("SENT") + 1] == "SETTLEMENT_UNKNOWN"
    # One authorization only. M5's reconcile may resend the IDENTICAL header once; it never signs a second one.
    assert len({q.headers["PAYMENT-SIGNATURE"] for q in seller.paid}) == 1 and len(seller.paid) <= 2
    assert deps.repo.spent_minor("test") == 500_000  # unknown still counts as spent


async def test_compare_and_set_loss_aborts_before_signing(deps, seller):
    def steal(_request):
        row = deps.repo.find_by_key("test", "i", fingerprint("report", {"ticker": "A"}))[0]
        assert deps.repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.ABANDONED)

    seller.on_unpaid = steal
    r = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert r["payment_status"] == "ABANDONED" and seller.paid == [] and "payment_payload" not in kinds(deps, r["purchase_id"])


async def test_same_intent_returns_existing_row_without_paying(deps, seller):
    a = await purchase(deps, "report", {"ticker": "A"}, "i")
    b = await purchase(deps, "report", {"ticker": "A"}, "i")
    assert a["purchase_id"] == b["purchase_id"] and len(seller.paid) == 1


async def test_convert_without_pdf_validator_is_unverified_not_failed(deps, seller):
    # M2 has no pdf_page_count validator yet: KeyError -> FULFILLMENT_UNVERIFIED (design §5). M4 changes this to FAILED.
    r = await purchase(deps, "convert", {"doc_id": "d", "pages": 20}, "i")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] in ("UNVERIFIED", "FAILED")
    assert r["artifact"]["path"].endswith(".pdf") and "json" not in r["artifact"]


async def test_unknown_service(deps):
    r = await purchase(deps, "nope", {}, "i")
    assert "error" in r and r["safe_next_actions"] == ["human_review"]
```

- [ ] **Step 3: Run the tests and see them fail**

Run: `uv run pytest tests/test_purchase_flow.py -q`
Expected: `ModuleNotFoundError: No module named 'billguard.purchase'`

- [ ] **Step 4: Write `billguard/outcome.py`**

```python
"""Paid request send and design §3 step 6 outcome classification. Shared by purchase.py and reconcile.py."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from x402.http.constants import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import decode_payment_required_header, decode_payment_response_header

from billguard import artifacts
from billguard.models import EvidenceKind, PaymentStatus, Purchase, history_entry

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    kind: str  # "response" | "error"
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased keys
    content: bytes = b""
    content_type: str = ""
    error: str | None = None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def payment_required_error(self) -> str | None:
        """The 402's PAYMENT-REQUIRED.error if decodable. Informational only; never decides money (design §5)."""
        h = self.header(PAYMENT_REQUIRED_HEADER)
        if not h:
            return None
        try:
            return decode_payment_required_header(h).error
        except Exception:  # noqa: BLE001
            return None


async def send_paid_request(http: httpx.AsyncClient, method: str, url: str, body: dict[str, Any] | None,
                            header: str, timeout_s: float) -> Outcome:
    try:
        resp = await http.request(method, url, json=body, headers={PAYMENT_SIGNATURE_HEADER: header}, timeout=timeout_s)
    except httpx.HTTPError as e:
        return Outcome("error", error=f"{type(e).__name__}: {e}")
    return Outcome("response", resp.status_code, {k.lower(): v for k, v in resp.headers.items()},
                   resp.content, resp.headers.get("content-type", ""))


def classify_outcome(repo: Any, row: Purchase, outcome: Outcome, from_state: PaymentStatus,
                     artifact_dir: Path | str, via: str = "send") -> Purchase:
    """Design §3 step 6. Only 2xx + decodable PAYMENT-RESPONSE with success=true is SETTLED.
    Everything else becomes SETTLEMENT_UNKNOWN. A 2xx body is stored as an artifact first."""
    pid = row.purchase_id
    fields: dict[str, Any] = {}
    if outcome.kind == "response" and 200 <= (outcome.status or 0) < 300:
        art = artifacts.store_artifact(outcome.content, outcome.content_type, pid, Path(artifact_dir))
        repo.add_evidence(pid, EvidenceKind.ARTIFACT, art.evidence())
        fields["artifact_hash"] = art.sha256
        hdr = outcome.header(PAYMENT_RESPONSE_HEADER)
        settle = None
        if hdr:
            try:
                settle = decode_payment_response_header(hdr)
            except Exception as e:  # noqa: BLE001
                repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                                  "reason": "PAYMENT-RESPONSE decode failed", "detail": f"{type(e).__name__}: {e}"})
                reason = "2xx with undecodable PAYMENT-RESPONSE"
            if settle is not None and settle.success:
                repo.add_evidence(pid, EvidenceKind.SETTLEMENT, {"transaction": settle.transaction, "network": settle.network,
                                  "payer": settle.payer, "amount": settle.amount, "via": via})
                repo.update_status(pid, from_state, PaymentStatus.SETTLED,
                                   history_entry("SETTLED", via=via, tx=settle.transaction),
                                   settlement_tx=settle.transaction, **fields)
                return repo.get_purchase(pid)
            if settle is not None:
                repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                                  "reason": "PAYMENT-RESPONSE success=false", "payment_response": settle.model_dump(mode="json")})
                reason = f"2xx with success=false ({settle.error_reason})"
        else:
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                              "reason": "2xx without PAYMENT-RESPONSE"})
            reason = "2xx without PAYMENT-RESPONSE"
    elif outcome.kind == "response":
        pr_error = outcome.payment_required_error()
        pay_resp: Any = None
        hdr = outcome.header(PAYMENT_RESPONSE_HEADER)
        if hdr:
            try:
                pay_resp = decode_payment_response_header(hdr).model_dump(mode="json")
            except Exception:  # noqa: BLE001
                pay_resp = {"undecodable": hdr[:64]}
        repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"status": outcome.status, "via": via,
                          "payment_required_error": pr_error, "payment_response": pay_resp,
                          "body": outcome.content[:2000].decode("utf-8", "replace")})
        reason = f"http {outcome.status}" + (f" ({pr_error})" if pr_error else "")
    else:
        repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"error": outcome.error, "via": via})
        reason = outcome.error or "transport error"
    repo.update_status(pid, from_state, PaymentStatus.SETTLEMENT_UNKNOWN,
                       history_entry("SETTLEMENT_UNKNOWN", via=via, reason=reason), **fields)
    return repo.get_purchase(pid)
```

- [ ] **Step 5: Write `billguard/purchase.py`**

```python
"""Purchase state machine (design §3). Every transition is persisted before the next network call."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from x402.client import x402Client
from x402.http.constants import PAYMENT_REQUIRED_HEADER
from x402.http.utils import (
    decode_payment_required_header,
    detect_payment_required_version,
    encode_payment_signature_header,
)
from x402.schemas import PaymentRequired, PaymentRequirements

from billguard import artifacts, guard, reconcile, verify
from billguard.guard import IntentDecision
from billguard.models import (
    EvidenceKind,
    FindingCode,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    RejectReason,
    fingerprint,
    history_entry,
)
from billguard.outcome import classify_outcome, send_paid_request
from billguard.policy import MandateError, Policy, check_mandate, check_service_allowed
from billguard.registry import Service
from billguard.response import build_response
from billguard.store.base import DuplicateKeyError, Repository

log = logging.getLogger(__name__)
P = PaymentStatus
LOCK = asyncio.Lock()  # process-wide: lookup + guard + row creation, and the budget read + AUTHORIZED write. Never held across network I/O.
CALL_BUDGET_S = 45.0  # review S4: wall-clock budget for one tool call


@dataclass
class Deps:
    repo: Repository
    policy: Policy
    registry: dict[str, Service]
    client_factory: Callable[[str], x402Client]
    http_factory: Callable[[Service], httpx.AsyncClient]
    artifact_dir: Path
    chain_reader: Callable[..., Awaitable[Any]] | None = None  # M5 default: billguard.chain.authorization_state
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


async def purchase(deps: Deps, service_id: str, args: dict[str, Any], purchase_intent_id: str,
                   task_id: str | None = None) -> dict[str, Any]:
    """Entry point for every paid tool (design §6)."""
    deadline = time.monotonic() + CALL_BUDGET_S
    service = deps.registry.get(service_id)
    if service is None:
        return {"error": f"unknown service '{service_id}'", "safe_next_actions": [NextAction.HUMAN_REVIEW]}
    fp = fingerprint(service_id, args)

    async with LOCK:
        decision, row = _resolve_and_create(deps, service, args, fp, purchase_intent_id, task_id)
    if decision.action == "return" and decision.reconcile_first and decision.row is not None:
        await reconcile_and_finish(deps, decision.row, deadline)  # network I/O: outside the lock
        async with LOCK:
            decision, row = _resolve_and_create(deps, service, args, fp, purchase_intent_id, task_id, after_reconcile=True)
    if decision.action == "return" or row is None:
        assert decision.row is not None
        return build_response(deps, deps.repo.get_purchase(decision.row.purchase_id))

    try:
        check_service_allowed(deps.policy, service)
    except MandateError as e:
        return build_response(deps, _reject(deps.repo, row, P.INTENT_RECORDED, e.code, str(e)))
    row = await run_from_quote(deps, row, service, args, deadline)
    return build_response(deps, row)


def _resolve_and_create(deps: Deps, service: Service, args: dict[str, Any], fp: str, intent_id: str,
                        task_id: str | None, after_reconcile: bool = False) -> tuple[IntentDecision, Purchase | None]:
    """Design §3 step 1 under the lock: decide, then create the row in the same critical section."""
    decision = guard.resolve_intent(deps, service, args, fp, intent_id, after_reconcile=after_reconcile)
    if decision.action != "proceed":
        return decision, None
    row = Purchase.new(principal_id=deps.policy.principal_id, task_id=task_id, purchase_intent_id=intent_id,
                       request_fingerprint=fp, service_id=service.id, args=args, network=service.network,
                       attempt=decision.attempt, supersedes_purchase_id=decision.supersedes)
    try:
        return decision, deps.repo.create_purchase(row)
    except DuplicateKeyError:
        existing = max(deps.repo.find_by_key(deps.policy.principal_id, intent_id, fp), key=lambda r: r.attempt)
        return IntentDecision("return", row=existing), None  # a concurrent call won; response shows IN_PROGRESS


def _reject(repo: Repository, row: Purchase, expected: PaymentStatus, code: str, summary: str,
            finding: bool = False, evidence_ids: list[str] | tuple[str, ...] = ()) -> Purchase:
    repo.update_status(row.purchase_id, expected, P.REJECTED,
                       history_entry("REJECTED", reason=str(code), detail=summary), reject_reason=str(code))
    if finding:
        actions = [NextAction.HUMAN_REVIEW] if str(code) == RejectReason.SERVICE_QUARANTINED else [NextAction.GET_PURCHASE]
        repo.add_finding(row.purchase_id, str(code), "proven", summary, list(evidence_ids), actions)
    return repo.get_purchase(row.purchase_id)


def _lost_race(repo: Repository, row: Purchase) -> Purchase:
    log.warning("compare-and-set lost for %s; aborting this call", row.purchase_id)
    return repo.get_purchase(row.purchase_id)


def clip_timeout(timeout_s: float, deadline: float) -> float:
    """Review S4: no single network wait may run past the 45 s call budget (floor 1 s so a request still goes out)."""
    return max(1.0, min(timeout_s, deadline - time.monotonic()))


def _select_option(pr: PaymentRequired, service: Service) -> PaymentRequirements | None:
    for opt in pr.accepts:
        if opt.scheme == "exact" and opt.network == service.network and str(opt.asset).lower() == service.asset.lower():
            return opt
    return None


async def run_from_quote(deps: Deps, row: Purchase, service: Service, args: dict[str, Any], deadline: float) -> Purchase:
    """Design §3 steps 2–7 on an INTENT_RECORDED row. Also used by scripts/approve_retry.py (M8)."""
    repo = deps.repo
    pid = row.purchase_id
    method, url, body = service.build_request(args)
    async with deps.http_factory(service) as http:
        # ---- 2. quote: unpaid request, version detection, decode, select option
        try:
            resp = await http.request(method, url, json=body, timeout=clip_timeout(service.timeout_s, deadline))
        except httpx.HTTPError as e:
            # A transient outage is not proof that the route is not x402: no finding, reject reason QUOTE_FAILED.
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "error": f"{type(e).__name__}: {e}"})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.QUOTE_FAILED,
                           f"unpaid request failed before any payment: {type(e).__name__}")
        try:
            version = detect_payment_required_version({k: v for k, v in resp.headers.items()}, resp.content)
        except ValueError:
            ev = repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "status": resp.status_code,
                                                                     "reason": "no x402 payment-required response"})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.NOT_X402,
                           f"route is not x402-protected (http {resp.status_code} without PAYMENT-REQUIRED)",
                           finding=True, evidence_ids=[ev])
        if version != 2:
            repo.add_evidence(pid, EvidenceKind.SELLER_ERROR, {"stage": "quote", "x402_version": version})
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.UNSUPPORTED_X402_VERSION,
                           f"x402 version {version} is not supported (v2 only)")
        pr = decode_payment_required_header(resp.headers[PAYMENT_REQUIRED_HEADER])
        quote_ev = repo.add_evidence(pid, EvidenceKind.QUOTE, pr.model_dump(mode="json"))
        selected = _select_option(pr, service)
        if selected is None:
            return _reject(repo, row, P.INTENT_RECORDED, RejectReason.PRICE_MISMATCH,
                           f"no 'exact' option on {service.network} paying canonical USDC {service.asset}",
                           finding=True, evidence_ids=[quote_ev])
        sel_ev = repo.add_evidence(pid, EvidenceKind.SELECTED_REQUIREMENT, selected.model_dump(mode="json"))
        amount = int(selected.amount)
        if not repo.update_status(pid, P.INTENT_RECORDED, P.QUOTED,
                                  history_entry("QUOTED", amount_minor=amount, pay_to=selected.pay_to),
                                  quoted_amount_minor=amount, asset=selected.asset, pay_to=selected.pay_to):
            return _lost_race(repo, row)

        # ---- 3. authorize: mandate under the lock so the budget read and the AUTHORIZED write are atomic
        async with LOCK:
            spent = repo.spent_minor(deps.policy.principal_id)
            try:
                check_mandate(deps.policy, service, amount, selected.asset, spent)
            except MandateError as e:
                return _reject(repo, row, P.QUOTED, e.code, str(e), finding=True, evidence_ids=[quote_ev, sel_ev])
            repo.add_evidence(pid, EvidenceKind.AUTHORIZATION, {
                "amount_minor": amount, "accepted_price_minor": service.accepted_price_minor,
                "spent_before_minor": spent, "remaining_after_minor": deps.policy.total_budget_minor - spent - amount,
                "per_purchase_cap_minor": deps.policy.per_purchase_cap_minor})
            if not repo.update_status(pid, P.QUOTED, P.AUTHORIZED, history_entry("AUTHORIZED", amount_minor=amount),
                                      authorized_amount_minor=amount):
                return _lost_race(repo, row)

        # ---- 4. sign: narrow accepts to the selected option, await, assert, encode, persist, then PAYLOAD_SIGNED
        try:
            client = deps.client_factory(service.network)
            payload = await client.create_payment_payload(pr.model_copy(update={"accepts": [selected]}))
            if payload.accepted != selected:
                raise RuntimeError("SDK selected a different payment option than BillGuard authorized")
            auth = payload.payload["authorization"]
            if int(auth["value"]) != amount:
                raise RuntimeError(f"signed value {auth['value']} != authorized {amount}")
            header = encode_payment_signature_header(payload)
        except Exception as e:  # noqa: BLE001
            log.exception("signing failed for %s", pid)
            return _reject(repo, row, P.AUTHORIZED, RejectReason.SIGNING_FAILED, f"{type(e).__name__}: {e}")
        repo.add_evidence(pid, EvidenceKind.PAYMENT_PAYLOAD, {
            "header": header, "from": auth["from"], "to": auth["to"], "value": auth["value"], "nonce": auth["nonce"],
            "validAfter": auth["validAfter"], "validBefore": auth["validBefore"], "network": service.network})
        valid_before = int(auth["validBefore"])
        if not repo.update_status(pid, P.AUTHORIZED, P.PAYLOAD_SIGNED, history_entry("PAYLOAD_SIGNED", valid_before=valid_before),
                                  valid_before=valid_before):
            return _lost_race(repo, row)

        # ---- 5. send: SENT is persisted BEFORE the paid request leaves (design §3)
        if not repo.update_status(pid, P.PAYLOAD_SIGNED, P.SENT, history_entry("SENT", timeout_s=service.timeout_s)):
            return _lost_race(repo, row)
        outcome = await send_paid_request(http, method, url, body, header, clip_timeout(service.timeout_s, deadline))

    # ---- 6. outcome
    row = classify_outcome(repo, repo.get_purchase(pid), outcome, P.SENT, deps.artifact_dir, via="send")
    if row.payment_status == P.SETTLEMENT_UNKNOWN:
        return await reconcile_and_finish(deps, row, deadline)
    # ---- 7. verify
    return _verify(deps, row, service)


async def reconcile_and_finish(deps: Deps, row: Purchase, deadline: float) -> Purchase:
    """Reconcile a SETTLEMENT_UNKNOWN row (design §5), then finish: unknown finding, or step-7 verification."""
    row = await reconcile.reconcile(deps, row, deadline)
    if row.payment_status == P.SETTLEMENT_UNKNOWN:
        _ensure_unknown_finding(deps.repo, row)
        return row
    if row.payment_status == P.SETTLED:
        deps.repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
        return _verify(deps, row, deps.registry[row.service_id])
    return row


def _ensure_unknown_finding(repo: Repository, row: Purchase) -> None:
    if any(f.code == FindingCode.SETTLEMENT_UNKNOWN and f.resolved_at is None for f in repo.findings_for_purchase(row.purchase_id)):
        return
    ev_ids = [e.evidence_id for e in repo.list_evidence(row.purchase_id)
              if e.kind in (EvidenceKind.SELLER_ERROR, EvidenceKind.CHAIN_CHECK, EvidenceKind.RESEND)]
    repo.add_finding(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN, "suspected",
                     f"Payment was sent to '{row.service_id}' but settlement could not be confirmed; "
                     f"the authorization is valid until {row.valid_before}", ev_ids[-3:],
                     [NextAction.RECONCILE, NextAction.GET_PURCHASE])


def _verify(deps: Deps, row: Purchase, service: Service) -> Purchase:
    """Design §3 step 7 / §5 validators. Idempotent: only runs on SETTLED rows with fulfillment PENDING."""
    repo = deps.repo
    if row.payment_status != P.SETTLED or row.fulfillment_status != FulfillmentStatus.PENDING:
        return row
    pid = row.purchase_id
    evidence = repo.list_evidence(pid)
    artifact = artifacts.load_latest(repo, pid)
    if artifact is None:
        proof = [e.evidence_id for e in evidence if e.kind in (EvidenceKind.SETTLEMENT, EvidenceKind.CHAIN_CHECK)]
        repo.set_fulfillment(pid, FulfillmentStatus.NO_DELIVERY, history_entry("NO_DELIVERY"))
        repo.add_finding(pid, FindingCode.PAID_NO_DELIVERY, "proven",
                         f"Settled {row.authorized_amount_minor} minor USDC to '{service.id}' but no artifact was delivered",
                         proof, [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])
        return repo.get_purchase(pid)
    art_ev = [e.evidence_id for e in evidence if e.kind == EvidenceKind.ARTIFACT][-1]
    try:
        results = verify.run_validators(service, row.args, artifact)
    except Exception as e:  # noqa: BLE001
        ev = repo.add_evidence(pid, EvidenceKind.VALIDATOR, {"name": "<runner>", "passed": None, "error": f"{type(e).__name__}: {e}"})
        repo.set_fulfillment(pid, FulfillmentStatus.UNVERIFIED, history_entry("UNVERIFIED", error=f"{type(e).__name__}: {e}"))
        repo.add_finding(pid, FindingCode.FULFILLMENT_UNVERIFIED, "suspected",
                         f"Validator raised {type(e).__name__}: {e}", [ev, art_ev], [NextAction.HUMAN_REVIEW])
        return repo.get_purchase(pid)
    failed: list[tuple[str, str, dict[str, Any]]] = []
    for name, passed, detail in results:
        ev = repo.add_evidence(pid, EvidenceKind.VALIDATOR, {"name": name, "passed": passed, **detail})
        if not passed:
            failed.append((name, ev, detail))
    if failed:
        repo.set_fulfillment(pid, FulfillmentStatus.FAILED, history_entry("FULFILLMENT_FAILED", validators=[n for n, _, _ in failed]))
        for name, ev, detail in failed:
            repo.add_finding(pid, FindingCode.FULFILLMENT_FAILED, "proven",
                             f"Validator '{name}' failed: {detail.get('reason', detail)}", [ev, art_ev],
                             [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])
    else:
        repo.set_fulfillment(pid, FulfillmentStatus.VERIFIED, history_entry("VERIFIED", validators=[n for n, _, _ in results]))
    return repo.get_purchase(pid)
```

- [ ] **Step 6: Run the tests and see them pass**

Run: `uv run pytest tests/test_purchase_flow.py -q`
Expected: all pass. If `test_happy_path...` fails on the evidence-kind order, check that `classify_outcome` stores the artifact before the settlement evidence and that `_verify` adds one validator row per registry validator (4 for `report`).

- [ ] **Step 7: Commit**

```bash
git add billguard/outcome.py billguard/purchase.py tests/conftest.py tests/test_purchase_flow.py
git commit -m "feat(core): purchase state machine with compare-and-set persistence and outcome classification"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_purchase_flow.py -q` → Expected: `passed`, 0 failed
- Run: `uv run pytest -q` → Expected: all pass
- Run: `grep -c 'await client.create_payment_payload' billguard/purchase.py` → Expected: `1`
- Run: `grep -n 'async with LOCK' billguard/purchase.py | wc -l | tr -d ' '` → Expected: `3` (two in `purchase`, one in `run_from_quote`), and none of them encloses `http.request` or `send_paid_request` (reviewer reads the code)
- Run: `grep -c 'P.PAYLOAD_SIGNED, P.SENT' billguard/purchase.py` → Expected: `1` and it appears textually before `send_paid_request(`
- Run: `grep -c 'clip_timeout(service.timeout_s, deadline)' billguard/purchase.py` → Expected: `2` (quote and send are both clipped to the 45 s budget)

**Manual Sepolia check:** covered by Task 2.7.

### Task 2.7: Runtime deps, FastMCP server, Claude Desktop wiring `[W-CORE]`

**Maps to:** F1 (four paid tools + five admin tools; the Bazaar tool is M7), F2 (no force-retry parameter, no `approve_retry` tool), F10 (recovery spawned after initialize), non-functional stdout protection (S7), design §6, §2 "FastMCP has no post-initialize callback" (asyncio.create_task after 2 s), metric 2.
**Timebox:** 15 min.
**§7 tests covered:** the schema test precondition (tool listing); stdout-protection probe.

**Files:**
- Create: `billguard/deps.py`, `billguard/server.py`

**SDK calls:** FastMCP `FastMCP(name, instructions=..., lifespan=...)`, `@mcp.tool(...)`, `mcp.run()` (stdio default).

**Implementer brief:** Wire everything into one stdio MCP server. `configure_logging()` runs before any other import that could print. The lifespan builds `Deps` and spawns `recovery.run_startup_recovery(deps)` with `asyncio.create_task` so the MCP initialize handshake is never blocked; the task sleeps 2 s before touching the store (the stub returns immediately for now; M3 fills it). Paid tools are thin typed wrappers over `purchase()`; **none takes any force/retry parameter**. Tool descriptions tell the agent to reuse `purchase_intent_id` and read `findings` before spending again. `reconcile_purchase` never creates a payment: it only calls `reconcile_and_finish`. `tier="premium"` on `get_company_report` routes to the `report_premium` service (Plan decision, Task 2.3). If the installed fastmcp version rejects `lifespan=` or `@mcp.tool(...)`, check `uv run python -c "import fastmcp; print(fastmcp.__version__)"` and adapt to that version's documented equivalent, then record the version in your report.

- [ ] **Step 1: Write `billguard/deps.py`**

```python
"""Build the runtime Deps once: env, store, policy, registry, wallet, HTTP client factory."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from billguard import wallet
from billguard.policy import load_policy
from billguard.purchase import Deps
from billguard.registry import load_registry
from billguard.store import open_repository

ROOT = Path(__file__).resolve().parents[1]
_DEPS: Deps | None = None


def _abs(env: str, default: str) -> Path:
    p = Path(os.environ.get(env, default))
    return p if p.is_absolute() else ROOT / p


def build_deps() -> Deps:
    load_dotenv(ROOT / ".env")
    return Deps(
        repo=open_repository(ROOT),
        policy=load_policy(_abs("BILLGUARD_POLICY", "policy.yaml")),
        registry=load_registry(),
        client_factory=wallet.build_client,
        http_factory=lambda svc: httpx.AsyncClient(timeout=svc.timeout_s),
        artifact_dir=_abs("BILLGUARD_ARTIFACT_DIR", "artifacts"),
    )


def get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = build_deps()
    return _DEPS
```

- [ ] **Step 2: Write `billguard/server.py`**

```python
"""BillGuard MCP server over stdio (design §6). stdout is JSON-RPC; all logs go to stderr."""
from __future__ import annotations

from billguard.logging_setup import configure_logging

configure_logging()  # before anything else can print

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # env-gated tool registration (M7) needs this at import time

from billguard import recovery  # noqa: E402
from billguard.deps import get_deps  # noqa: E402
from billguard.models import PaymentStatus  # noqa: E402
from billguard.purchase import CALL_BUDGET_S, purchase, reconcile_and_finish  # noqa: E402
from billguard.response import build_response  # noqa: E402

log = logging.getLogger("billguard.server")

INSTRUCTIONS = (
    "BillGuard buys from x402 sellers on your behalf and verifies what was delivered. "
    "Always pass a purchase_intent_id you made up for the task, and REUSE the same purchase_intent_id when you retry "
    "the same purchase. Read `findings` and `safe_next_actions` in every result before spending again. "
    "BillGuard never pays twice for the same purchase and has no tool that can make a second payment; a human must approve that."
)
REUSE = ("Reuse the same purchase_intent_id when retrying the same purchase. Check `findings` and `safe_next_actions` "
         "in the result before spending again.")


@asynccontextmanager
async def lifespan(_server: FastMCP):
    deps = get_deps()

    async def _recover() -> None:
        await asyncio.sleep(2)  # let the initialize handshake finish first (review S7)
        try:
            await recovery.run_startup_recovery(deps)
        except Exception:  # noqa: BLE001
            log.exception("startup recovery failed")

    task = asyncio.create_task(_recover())
    try:
        yield {}
    finally:
        task.cancel()


mcp = FastMCP("billguard", instructions=INSTRUCTIONS, lifespan=lifespan)


# ---- paid tools (design §6): (<service args>, purchase_intent_id, task_id). No force/retry parameter exists. ----

@mcp.tool(description=f"Buy a company report for a ticker: $0.50 USDC on Base Sepolia (tier='premium' asks the $0.75 route). {REUSE}")
async def get_company_report(ticker: str, purchase_intent_id: str, task_id: str | None = None, tier: str = "standard") -> dict[str, Any]:
    service_id = "report_premium" if tier == "premium" else "report"
    return await purchase(get_deps(), service_id, {"ticker": ticker.strip().upper()}, purchase_intent_id, task_id)


@mcp.tool(description=f"Buy a research report on a topic: $0.75 USDC on Base Sepolia. {REUSE}")
async def get_research_report(topic: str, purchase_intent_id: str, task_id: str | None = None) -> dict[str, Any]:
    return await purchase(get_deps(), "research", {"topic": topic.strip()}, purchase_intent_id, task_id)


@mcp.tool(description=f"Convert a document to a PDF with the given page count: $2.00 USDC on Base Sepolia. {REUSE}")
async def convert_document(doc_id: str, pages: int, purchase_intent_id: str, task_id: str | None = None) -> dict[str, Any]:
    return await purchase(get_deps(), "convert", {"doc_id": doc_id.strip(), "pages": int(pages)}, purchase_intent_id, task_id)


# The fourth paid tool (public Bazaar seller on Base mainnet) is registered in M7 (Task 7.1).


# ---- admin tools (design §6). None can approve a second payment (review S8). ----

@mcp.tool(description="List the allowlisted services with their accepted prices, networks and validators.")
async def list_services() -> list[dict[str, Any]]:
    return [s.to_dict() for s in get_deps().registry.values()]


@mcp.tool(description="Show the spending mandate (budget, per-purchase cap, allowed services) and what has been spent.")
async def get_spending_policy() -> dict[str, Any]:
    deps = get_deps()
    spent = deps.repo.spent_minor(deps.policy.principal_id)
    return {**deps.policy.to_dict(), "spent_minor": spent, "remaining_minor": deps.policy.total_budget_minor - spent, "currency": "USDC"}


@mcp.tool(description="Get a purchase record with its history, evidence-backed findings and safe next actions.")
async def get_purchase(purchase_id: str) -> dict[str, Any]:
    deps = get_deps()
    row = deps.repo.get_purchase(purchase_id)
    if row is None:
        return {"error": f"purchase {purchase_id} not found"}
    return build_response(deps, row)


@mcp.tool(description="List findings (price mismatches, unknown settlements, missing or incomplete deliveries, blocked duplicates) with their evidence.")
async def list_findings(unresolved_only: bool = True) -> list[dict[str, Any]]:
    deps = get_deps()
    return deps.repo.list_findings(deps.policy.principal_id, unresolved_only=unresolved_only)


@mcp.tool(description="Re-check an unknown settlement on-chain. Never creates a new payment; at most one identical resend.")
async def reconcile_purchase(purchase_id: str) -> dict[str, Any]:
    deps = get_deps()
    row = deps.repo.get_purchase(purchase_id)
    if row is None:
        return {"error": f"purchase {purchase_id} not found"}
    if row.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
        row = await reconcile_and_finish(deps, row, time.monotonic() + CALL_BUDGET_S)
    return build_response(deps, row)


if __name__ == "__main__":
    mcp.run()  # stdio
```

- [ ] **Step 3: Boot the server once over stdio and list tools**

Run: `uv run python -c "
import asyncio
from billguard.server import mcp
async def main():
    tools = await mcp.get_tools()
    names = sorted(tools)
    print(names)
    for n in ('get_company_report','get_research_report','convert_document'):
        props = set(tools[n].parameters['properties'])
        assert 'purchase_intent_id' in props and not any('force' in p or 'retry' in p for p in props), (n, props)
    assert 'approve_retry' not in names
    print('schema ok')
asyncio.run(main())"`
Expected: a sorted list containing `convert_document, get_company_report, get_purchase, get_research_report, get_spending_policy, list_findings, list_services, reconcile_purchase`, then `schema ok`. (If `get_tools` does not exist in the installed fastmcp, use that version's documented tool listing and record it; the M3 schema test must use the same call.)

Run: `(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}\n'; sleep 3) | uv run python -m billguard.server 2>/dev/null | head -c 400`
Expected: one JSON-RPC line starting with `{"jsonrpc":"2.0","id":1,"result":` and nothing else on stdout (logs went to stderr, which is discarded here).

- [ ] **Step 4: Register in Claude Desktop (operator)**

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and add, with absolute paths (`which uv` for the first one):
```json
{
  "mcpServers": {
    "billguard": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/pay-guard", "python", "-m", "billguard.server"]
    }
  }
}
```
Restart Claude Desktop. The seller must be running (`uv run uvicorn sellers.app:app --port 8402`).

- [ ] **Step 5: Commit**

```bash
git add billguard/deps.py billguard/server.py
git commit -m "feat(server): FastMCP stdio server with paid and admin tools, background recovery task"
```

**Exit criteria (auto):**
- Step 3's schema command prints `schema ok`.
- Run: `grep -c 'force' billguard/server.py` → Expected: `1` (only the comment "No force/retry parameter exists")
- Run: `grep -c 'def approve_retry' billguard/server.py` → Expected: `0`
- Run: `grep -c 'asyncio.create_task' billguard/server.py` → Expected: `1`
- Run: `grep -rn 'print(' billguard/ | wc -l | tr -d ' '` → Expected: `0`

**Manual Sepolia check (M2 checkpoint, metric 2):** in Claude Desktop, prompt: *"Use BillGuard to buy the company report for ticker ACME. Use purchase intent id demo-report-1."* The tool result shows `payment_status: SETTLED`, `fulfillment_status: VERIFIED`, a `settlement_tx`, and an `explorer_url` that opens a Sepolia USDC transfer of 0.50 to the seller address.

### Task 2.8: `scripts/reset_demo.py --yes` `[W-CORE]`

**Maps to:** F12, design §8 `reset_demo.py` (deletes the principal's purchases, evidence and findings; prints remaining USDC balances; refuses without `--yes`), §9 pre-demo step, §10 stale-state risk.
**Timebox:** 5 min.
**SDK calls:** none.
**§7 tests covered:** none (operator script; design §8 reset semantics).

**Files:**
- Create: `scripts/reset_demo.py`

**Implementer brief:** Rehearsals start now, so the reset script is needed now. It wipes BillGuard's ledger for the configured principal on the active store (which also clears the 15-minute duplicate guard and the spend), prints what it deleted and the remaining USDC balances, and refuses to run without `--yes`. On-chain transfers are never undone; artifact files on disk are left alone.

- [ ] **Step 1: Write the script**

```python
"""Wipe BillGuard demo state between rehearsals (design §8). Refuses to run without --yes.

Deletes the configured principal's purchases, evidence and findings on the active store
(BILLGUARD_STORE). Clears the 15-minute duplicate guard and the per-principal spend.
On-chain transfers are not undone. Artifact files are left on disk.
"""
from __future__ import annotations

import argparse
import sys

from billguard.deps import get_deps
from scripts.setup_wallets import main as wallets_main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="required; actually delete")
    args = ap.parse_args()
    if not args.yes:
        print("refusing to reset without --yes")
        return 2
    deps = get_deps()
    counts = deps.repo.reset(deps.policy.principal_id)
    print(f"reset principal {deps.policy.principal_id}: {counts}")
    print(f"spent now: {deps.repo.spent_minor(deps.policy.principal_id)} minor; budget {deps.policy.total_budget_minor} minor")
    print("balances:")
    sys.argv = ["setup_wallets.py", "--check"]
    return 0 if wallets_main() in (0, 1) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it**

Run: `uv run python scripts/reset_demo.py; echo "exit=$?"`
Expected: `refusing to reset without --yes` and `exit=2`.

Run: `uv run python scripts/reset_demo.py --yes`
Expected: `reset principal demo-agent: {'purchases': N, 'evidence': M, 'findings': K}` followed by `spent now: 0 minor ...` and the balances block.

- [ ] **Step 3: Commit**

```bash
git add scripts/reset_demo.py
git commit -m "feat(scripts): reset_demo wipes the principal's ledger between rehearsals"
```

**Exit criteria (auto):**
- Run: `uv run python scripts/reset_demo.py >/dev/null; echo $?` → Expected: `2`
- Run: `uv run python scripts/reset_demo.py --yes | grep -c 'spent now: 0 minor'` → Expected: `1`

**Manual Sepolia check:** after a reset, `get_spending_policy` in Claude Desktop shows `spent_minor: 0`.

### Milestone M2 — Checkpoint and manual verification

**Checkpoint (§8.2):** Claude buys a report.

Operator verification list:
- [ ] `uv run pytest -q` is green (models, store, policy, verify, purchase flow).
- [ ] The stdio probe in Task 2.7 step 3 shows exactly one JSON-RPC line on stdout.
- [ ] Claude Desktop lists the `billguard` server with 8 tools; none of the paid tools has a `force`/`retry` parameter; there is no `approve_retry` tool.
- [ ] Prompt *"buy the company report for ACME, purchase intent id demo-report-1"* returns `SETTLED` + `VERIFIED` + `settlement_tx`; the explorer link shows 0.50 USDC to the seller.
- [ ] Prompt *"buy it again with the same purchase intent id"* returns the same `purchase_id` and no second transfer on the explorer.
- [ ] `get_spending_policy` shows `spent_minor: 500000`; `list_findings` is empty.
- [ ] `uv run python scripts/reset_demo.py --yes` prints counts and `spent now: 0 minor`.
- [ ] Restart Claude Desktop once: the server comes back and `list_services` works (the lifespan recovery task did not block the handshake).

---

## Milestones M3 ∥ M4 ∥ M5 — independent once M2 lands (design §8: "Steps 3–5 are independent once step 2 lands")

Three people: run all three at once (00:20–02:00). One person: M3 (60 min) → M4 (45 min) → M5 (90 min). Each milestone edits only the files in its ownership row (§0.3). None of them edits `purchase.py`, `outcome.py`, `response.py`, `server.py`, `models.py` or `store/*`. If an implementer believes a change there is needed, it reports `BLOCKED` and the controller decides.

---

## Milestone M3 — Mandate and idempotency `[W-MANDATE]`

**Timebox:** 60 min. **Cut if overrun:** `test_duplicate_guard_window_is_15_minutes` and `test_recovery_is_idempotent` can be dropped; the guard's two duplicate variants (S1), quarantine (S3), recovery's `ABANDONED` path and the schema test cannot.
**Checkpoint (§8.3):** a `PRICE_MISMATCH` rejection, and a fresh intent ID gets `DUPLICATE_SUSPECTED` with no payment.

### Task 3.1: Full intent decision table: same-intent retry, service quarantine, duplicate guard, attempt+1 `[W-MANDATE]`

**Maps to:** F7 (duplicate guard both variants, 15-minute window, `SERVICE_QUARANTINED`), design §3 step 1 (all bullets, S1, S3), §7 tests "duplicate guard", "attempt+1 after PAYMENT_FAILED", "duplicate beat in both variants", "SERVICE_QUARANTINED after unresolved PAID_NO_DELIVERY", metric 4 (retry refused), PRD user story "same purchase again → existing record + DUPLICATE_SUSPECTED".
**Timebox:** 35 min.
**SDK calls:** none directly (DB only under the lock); the flow test drives `purchase()` and therefore `await client.create_payment_payload`.
**§7 tests covered:** "the duplicate guard", "attempt+1 after PAYMENT_FAILED", "the duplicate beat in both variants (S1)", "SERVICE_QUARANTINED after an unresolved PAID_NO_DELIVERY (S3)".

**Files:**
- Modify: `billguard/guard.py` (replace the stub body; keep the signature)
- Test: `tests/test_guard.py`

**Implementer brief:** Replace the M2 stub of `resolve_intent` with design §3 step 1 exactly. The function runs **under the process lock** and does DB work only (no network): the caller handles reconciliation when you return `reconcile_first=True`, then calls you again with `after_reconcile=True`. Decision table for an existing `(principal, intent, fingerprint)` key, looking at the latest attempt: `SETTLEMENT_UNKNOWN` and not `after_reconcile` → return with `reconcile_first=True`; `PAYMENT_FAILED` or `ABANDONED` → proceed with `attempt+1` and `supersedes` set (no money moved); in flight → return it (the response shows `IN_PROGRESS`); `SETTLEMENT_UNKNOWN` (still) or `SETTLED` with fulfillment not `VERIFIED` → return it, attach `DUPLICATE_SUSPECTED` once, append a `retry_blocked` evidence row every hit (S1); `SETTLED`+`VERIFIED` or `REJECTED` → return unchanged. For a new key: first the service quarantine (S3): if the service has an unresolved `PAID_NO_DELIVERY` finding for this principal, create the row and reject it `SERVICE_QUARANTINED` with a finding and `safe_next_actions: [human_review]`, no quote, no payment; then the duplicate guard: any row with the same `(principal, fingerprint)` from the last 15 minutes that is in flight, `SETTLEMENT_UNKNOWN`, or `SETTLED`-not-`VERIFIED` is returned with `DUPLICATE_SUSPECTED` (if it is `SETTLEMENT_UNKNOWN` and not `after_reconcile`, ask for reconciliation first). A verified purchase under a fresh intent pays again by design. The quarantine also applies to `attempt+1` rows (stricter than the letter of §3 but inside its intent: "new purchases from it are rejected"). Order matters: the duplicate guard runs **before** the quarantine so the same topic under a new intent id returns the existing record (Appendix B.11). A same-key row that was `REJECTED/SERVICE_QUARANTINED` may proceed as `attempt+1` once the quarantine is lifted (nothing was signed). In-flight same-key repeats also get the finding (PRD F7, Appendix B.10) while the response shows `IN_PROGRESS`.

- [ ] **Step 1: Write the failing tests**

`tests/test_guard.py`:
```python
import time

import pytest

from billguard.guard import resolve_intent
from billguard.models import FindingCode, FulfillmentStatus, PaymentStatus, Purchase, fingerprint
from billguard.purchase import purchase
from tests.conftest import FakeSeller

P = PaymentStatus
FP = fingerprint("report", {"ticker": "ACME"})


def drive(repo, row, *states, amount=500_000):
    cur = row.payment_status
    for st in states:
        fields = {}
        if st == P.QUOTED:
            fields = {"quoted_amount_minor": amount}
        if st == P.AUTHORIZED:
            fields = {"authorized_amount_minor": amount}
        if st == P.PAYLOAD_SIGNED:
            fields = {"valid_before": int(time.time()) + 300}
        assert repo.update_status(row.purchase_id, cur, st, **fields), f"{cur}->{st}"
        cur = st
    return repo.get_purchase(row.purchase_id)


def seed(repo, status, fulfillment=FulfillmentStatus.PENDING, intent="i1", fp=FP, service="report", attempt=1, age_s=0):
    row = repo.create_purchase(Purchase.new(principal_id="test", task_id=None, purchase_intent_id=intent, request_fingerprint=fp,
                                            service_id=service, args={"ticker": "ACME"}, network="eip155:84532", attempt=attempt))
    path = {
        P.INTENT_RECORDED: (), P.QUOTED: (P.QUOTED,), P.AUTHORIZED: (P.QUOTED, P.AUTHORIZED),
        P.PAYLOAD_SIGNED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED), P.SENT: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT),
        P.SETTLED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLED),
        P.SETTLEMENT_UNKNOWN: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN),
        P.PAYMENT_FAILED: (P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT, P.SETTLEMENT_UNKNOWN, P.PAYMENT_FAILED),
        P.REJECTED: (P.QUOTED, P.REJECTED), P.ABANDONED: (P.ABANDONED,),
    }[status]
    row = drive(repo, row, *path)
    if fulfillment != FulfillmentStatus.PENDING:
        repo.set_fulfillment(row.purchase_id, fulfillment)
    if age_s:
        repo.conn.execute("UPDATE purchases SET created_at = ?, updated_at = ? WHERE purchase_id = ?",
                          (time.time() - age_s, time.time() - age_s, row.purchase_id))
    return repo.get_purchase(row.purchase_id)


def svc(deps):
    return deps.registry["report"]


def test_new_key_proceeds(deps):
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "proceed" and d.attempt == 1 and d.supersedes is None


@pytest.mark.parametrize("status,fulfillment", [(P.SETTLED, FulfillmentStatus.VERIFIED), (P.REJECTED, FulfillmentStatus.PENDING)])
def test_same_key_terminal_rows_return_unchanged(deps, status, fulfillment):
    row = seed(deps.repo, status, fulfillment)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id
    assert deps.repo.findings_for_purchase(row.purchase_id) == []


def test_same_key_in_flight_returns_in_progress_with_duplicate_finding(deps):
    # PRD F7 / design §0 S1: in-flight repeats get DUPLICATE_SUSPECTED too; the response still shows IN_PROGRESS.
    row = seed(deps.repo, P.SENT)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id and not d.reconcile_first
    assert d.row.payment_status == P.SENT
    assert [f.code for f in deps.repo.findings_for_purchase(row.purchase_id)] == ["DUPLICATE_SUSPECTED"]
    from billguard.response import build_response
    assert build_response(deps, d.row)["payment_status"] == "IN_PROGRESS"


def test_same_key_unknown_asks_for_reconcile_then_marks_duplicate(deps):
    row = seed(deps.repo, P.SETTLEMENT_UNKNOWN)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.reconcile_first and deps.repo.findings_for_purchase(row.purchase_id) == []
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1", after_reconcile=True)
    assert d.action == "return" and not d.reconcile_first
    f = deps.repo.findings_for_purchase(row.purchase_id)
    assert [x.code for x in f] == ["DUPLICATE_SUSPECTED"] and f[0].severity == "suspected"
    assert f[0].safe_next_actions == ["human_review", "report_to_seller"]
    # a second hit appends evidence but not a second finding (S1)
    resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1", after_reconcile=True)
    assert [x.code for x in deps.repo.findings_for_purchase(row.purchase_id)] == ["DUPLICATE_SUSPECTED"]
    assert [e.kind for e in deps.repo.list_evidence(row.purchase_id)].count("retry_blocked") == 2


def test_same_key_settled_not_verified_is_duplicate(deps):
    row = seed(deps.repo, P.SETTLED, FulfillmentStatus.NO_DELIVERY)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id
    assert deps.repo.has_finding(row.purchase_id, FindingCode.DUPLICATE_SUSPECTED)


@pytest.mark.parametrize("status", [P.PAYMENT_FAILED, P.ABANDONED])
def test_same_key_proven_failure_creates_next_attempt(deps, status):
    row = seed(deps.repo, status)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "i1")
    assert d.action == "proceed" and d.attempt == 2 and d.supersedes == row.purchase_id


def test_new_intent_same_fingerprint_recent_unverified_is_duplicate(deps):
    row = seed(deps.repo, P.SETTLED, FulfillmentStatus.FAILED, intent="old")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "return" and d.row.purchase_id == row.purchase_id
    f = deps.repo.findings_for_purchase(row.purchase_id)
    assert [x.code for x in f] == ["DUPLICATE_SUSPECTED"]
    ev = [e for e in deps.repo.list_evidence(row.purchase_id) if e.kind == "retry_blocked"]
    assert ev[-1].payload["variant"] == "new_intent" and ev[-1].payload["purchase_intent_id"] == "new"


def test_new_intent_same_fingerprint_unknown_reconciles_first(deps):
    seed(deps.repo, P.SETTLEMENT_UNKNOWN, intent="old")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "return" and d.reconcile_first


def test_new_intent_verified_pays_again_by_design(deps):
    seed(deps.repo, P.SETTLED, FulfillmentStatus.VERIFIED, intent="old")
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "proceed"


def test_duplicate_guard_window_is_15_minutes(deps):
    seed(deps.repo, P.SETTLED, FulfillmentStatus.FAILED, intent="old", age_s=16 * 60)
    d = resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new")
    assert d.action == "proceed"


def test_service_quarantine_rejects_new_purchase(deps):
    bad = seed(deps.repo, P.SETTLED, FulfillmentStatus.NO_DELIVERY, intent="old", fp="x" * 64, service="research")
    deps.repo.add_finding(bad.purchase_id, FindingCode.PAID_NO_DELIVERY, "proven", "no artifact", [], ["human_review"])
    d = resolve_intent(deps, deps.registry["research"], {"topic": "reworded"}, fingerprint("research", {"topic": "reworded"}), "new")
    assert d.action == "return" and d.row.payment_status == P.REJECTED and d.row.reject_reason == "SERVICE_QUARANTINED"
    f = deps.repo.findings_for_purchase(d.row.purchase_id)
    assert [x.code for x in f] == ["SERVICE_QUARANTINED"] and f[0].safe_next_actions == ["human_review"]
    assert f[0].evidence_ids and deps.repo.list_evidence(d.row.purchase_id)[0].kind == "retry_blocked"  # evidence on its own row
    # the SAME topic under a new intent id is a duplicate hit on the existing record, not a quarantine row
    same_topic_fp = "x" * 64
    d2 = resolve_intent(deps, deps.registry["research"], {"topic": "same"}, same_topic_fp, "new-b")
    assert d2.row.purchase_id == bad.purchase_id and deps.repo.has_finding(bad.purchase_id, FindingCode.DUPLICATE_SUSPECTED)
    # other services are unaffected
    assert resolve_intent(deps, svc(deps), {"ticker": "ACME"}, FP, "new2").action == "proceed"
    # resolving the finding lifts the quarantine, also for the key that was quarantined (attempt+1)
    deps.repo.resolve_service_findings("test", "research", FindingCode.PAID_NO_DELIVERY)
    assert resolve_intent(deps, deps.registry["research"], {"topic": "again"}, fingerprint("research", {"topic": "again"}), "new3").action == "proceed"
    d3 = resolve_intent(deps, deps.registry["research"], {"topic": "reworded"}, fingerprint("research", {"topic": "reworded"}), "new")
    assert d3.action == "proceed" and d3.attempt == 2 and d3.supersedes == d.row.purchase_id


def signatures(seller):
    return {q.headers["PAYMENT-SIGNATURE"] for q in seller.paid}


async def test_flow_same_intent_and_new_intent_both_blocked_without_payment(make_deps):
    # SETTLEMENT_UNKNOWN row. With the M2 reconcile stub nothing is resent; with M5's real reconcile and the conftest
    # `never_used` reader, each reconcile resends the IDENTICAL header once. Either way only ONE authorization is ever signed.
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller)
    first = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    assert first["payment_status"] == "SETTLEMENT_UNKNOWN" and len(signatures(seller)) == 1
    same = await purchase(deps, "report", {"ticker": "ACME"}, "i1")
    assert same["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in same["findings"]]
    new = await purchase(deps, "report", {"ticker": "ACME"}, "i2")
    assert new["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in new["findings"]]
    assert len(signatures(seller)) == 1  # never a second signed authorization
    assert set(new["safe_next_actions"]) >= {"human_review", "report_to_seller"}
    assert not any("pay" in a or "retry" in a for a in new["safe_next_actions"])
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run pytest tests/test_guard.py -q`
Expected: failures in the same-key unknown/duplicate/attempt+1 tests, the new-intent duplicate tests, the quarantine test and the flow test. Six tests already pass against the M2 stub (`test_new_key_proceeds`, both terminal-row cases, `test_new_intent_verified_pays_again_by_design`, `test_duplicate_guard_window_is_15_minutes`) plus the in-flight test's first two assertions; that is expected.

- [ ] **Step 3: Replace `billguard/guard.py`**

```python
"""Design §3 step 1: intent lookup, service quarantine (S3), duplicate guard, same-intent retry (S1).

Runs under the process lock. DB work only: no network. When a SETTLEMENT_UNKNOWN row must be
reconciled first, the decision says reconcile_first=True; the caller reconciles outside the lock
and calls again with after_reconcile=True.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from billguard.models import (
    IN_FLIGHT,
    EvidenceKind,
    FindingCode,
    FulfillmentStatus,
    NextAction,
    PaymentStatus,
    Purchase,
    RejectReason,
    history_entry,
    iso_now,
    now,
)
from billguard.registry import Service

log = logging.getLogger(__name__)
P = PaymentStatus
DUPLICATE_WINDOW_S = 15 * 60


@dataclass
class IntentDecision:
    action: str  # "proceed" | "return"
    row: Purchase | None = None
    attempt: int = 1
    supersedes: str | None = None
    reconcile_first: bool = False  # caller reconciles `row` outside the lock, then calls again with after_reconcile=True


def _trips_guard(row: Purchase) -> bool:
    """Rows that must not be paid for again: in flight, unknown, or settled but not verified."""
    if row.payment_status in IN_FLIGHT or row.payment_status == P.SETTLEMENT_UNKNOWN:
        return True
    return row.payment_status == P.SETTLED and row.fulfillment_status != FulfillmentStatus.VERIFIED


def _mark_duplicate(deps: Any, row: Purchase, variant: str, purchase_intent_id: str) -> None:
    """S1: one DUPLICATE_SUSPECTED finding per row, one retry_blocked evidence row per hit."""
    repo = deps.repo
    ev = repo.add_evidence(row.purchase_id, EvidenceKind.RETRY_BLOCKED,
                           {"variant": variant, "purchase_intent_id": purchase_intent_id, "at": iso_now(),
                            "payment_status": str(row.payment_status), "fulfillment_status": str(row.fulfillment_status)})
    if not repo.has_finding(row.purchase_id, FindingCode.DUPLICATE_SUSPECTED):
        repo.add_finding(row.purchase_id, FindingCode.DUPLICATE_SUSPECTED, "suspected",
                         f"A repeat of purchase {row.purchase_id} ({row.payment_status}/{row.fulfillment_status}) was blocked; "
                         f"no second payment was made", [ev], [NextAction.HUMAN_REVIEW, NextAction.REPORT_TO_SELLER])
    repo.append_history(row.purchase_id, history_entry("retry_blocked", variant=variant, purchase_intent_id=purchase_intent_id))


def _quarantine(deps: Any, service: Service, args: dict[str, Any], fp: str, purchase_intent_id: str, attempt: int,
                supersedes: str | None) -> Purchase:
    """S3: create the row and reject it before any quote. The finding cites the unresolved PAID_NO_DELIVERY findings."""
    repo = deps.repo
    row = repo.create_purchase(Purchase.new(principal_id=deps.policy.principal_id, task_id=None, purchase_intent_id=purchase_intent_id,
                                            request_fingerprint=fp, service_id=service.id, args=args, network=service.network,
                                            attempt=attempt, supersedes_purchase_id=supersedes))
    prior = [f for f in repo.list_findings(deps.policy.principal_id, unresolved_only=True)
             if f["code"] == FindingCode.PAID_NO_DELIVERY and f["service_id"] == service.id]
    summary = (f"Service '{service.id}' is quarantined: {len(prior)} unresolved PAID_NO_DELIVERY finding(s) "
               f"(purchase {prior[0]['purchase_id'] if prior else '?'}). No quote was requested and nothing was paid.")
    # Every finding references at least one evidence row on ITS OWN purchase (PRD non-functional).
    own_ev = repo.add_evidence(row.purchase_id, EvidenceKind.RETRY_BLOCKED, {
        "variant": "quarantined", "service_id": service.id, "at": iso_now(),
        "blocking_findings": [{"finding_id": f["finding_id"], "purchase_id": f["purchase_id"], "evidence_ids": f["evidence_ids"]} for f in prior]})
    evidence_ids = [own_ev]
    repo.update_status(row.purchase_id, P.INTENT_RECORDED, P.REJECTED,
                       history_entry("REJECTED", reason=str(RejectReason.SERVICE_QUARANTINED), detail=summary),
                       reject_reason=str(RejectReason.SERVICE_QUARANTINED))
    repo.add_finding(row.purchase_id, FindingCode.SERVICE_QUARANTINED, "proven", summary, evidence_ids, [NextAction.HUMAN_REVIEW])
    return repo.get_purchase(row.purchase_id)


def resolve_intent(deps: Any, service: Service, args: dict[str, Any], request_fingerprint: str,
                   purchase_intent_id: str, after_reconcile: bool = False) -> IntentDecision:
    repo = deps.repo
    principal = deps.policy.principal_id
    attempt, supersedes = 1, None

    # ---- existing key: look at the latest attempt (design §3 step 1, first bullet) ----
    rows = repo.find_by_key(principal, purchase_intent_id, request_fingerprint)
    if rows:
        latest = max(rows, key=lambda r: r.attempt)
        st = latest.payment_status
        if st == P.SETTLEMENT_UNKNOWN and not after_reconcile:
            return IntentDecision("return", row=latest, reconcile_first=True)
        if st in (P.PAYMENT_FAILED, P.ABANDONED) or (st == P.REJECTED and latest.reject_reason == RejectReason.SERVICE_QUARANTINED):
            attempt, supersedes = latest.attempt + 1, latest.purchase_id  # proven: no money moved (Sugg. 6); a lifted quarantine may retry
        elif _trips_guard(latest):
            # In flight, unknown, or settled-not-verified: PRD F7 / S1 attach DUPLICATE_SUSPECTED on every variant.
            # For an in-flight row the response still shows IN_PROGRESS (response.py maps IN_FLIGHT states).
            _mark_duplicate(deps, latest, "same_intent", purchase_intent_id)
            return IntentDecision("return", row=repo.get_purchase(latest.purchase_id))
        else:
            return IntentDecision("return", row=latest)  # SETTLED+VERIFIED or REJECTED: unchanged

    # ---- duplicate guard on (principal, fingerprint) within 15 minutes, any intent id ----
    # Runs BEFORE the quarantine so a same-topic retry under a new intent id returns the EXISTING record with
    # DUPLICATE_SUSPECTED (PRD metric 4, user story); only a reworded request reaches the quarantine.
    if attempt == 1:
        for cand in repo.find_recent_by_fingerprint(principal, request_fingerprint, now() - DUPLICATE_WINDOW_S):
            if cand.purchase_intent_id == purchase_intent_id:
                continue
            if not _trips_guard(cand):
                continue  # a verified purchase under a fresh intent pays again by design (Sugg. 9)
            if cand.payment_status == P.SETTLEMENT_UNKNOWN and not after_reconcile:
                return IntentDecision("return", row=cand, reconcile_first=True)
            _mark_duplicate(deps, cand, "new_intent", purchase_intent_id)
            return IntentDecision("return", row=repo.get_purchase(cand.purchase_id))

    # ---- service quarantine (S3): before any quote for a new row ----
    if repo.has_unresolved_service_finding(principal, service.id, FindingCode.PAID_NO_DELIVERY):
        row = _quarantine(deps, service, args, request_fingerprint, purchase_intent_id, attempt, supersedes)
        return IntentDecision("return", row=row)

    return IntentDecision("proceed", attempt=attempt, supersedes=supersedes)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest tests/test_guard.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass (the M2 suite still holds; `test_same_intent_returns_existing_row_without_paying` still passes because a verified row returns unchanged).

- [ ] **Step 5: Commit**

```bash
git add billguard/guard.py tests/test_guard.py
git commit -m "feat(guard): same-intent retry, duplicate guard, service quarantine, attempt+1 after proven failure"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_guard.py -q` → Expected: `passed`, 0 failed
- Run: `uv run pytest -q` → Expected: all pass
- Run: `grep -c 'DUPLICATE_WINDOW_S = 15 \* 60' billguard/guard.py` → Expected: `1`
- Run: `grep -c 'httpx\|aiohttp\|requests' billguard/guard.py` → Expected: `0` (no network under the lock)

**Manual Sepolia check (M3 checkpoint part 2):** in Claude Desktop with the seller running: *"Convert document d1 to a 20-page PDF, purchase intent id demo-convert-1"* (settles, not VERIFIED because `/convert` returns 12 pages). Then *"Convert document d1 to a 20-page PDF again, use a new purchase intent id demo-convert-2"* → the result has the **same** `purchase_id`, a `DUPLICATE_SUSPECTED` finding, `safe_next_actions` of `human_review`/`report_to_seller`, and the explorer shows exactly one 2.00 USDC transfer.

### Task 3.2: Startup recovery `[W-MANDATE]`

**Maps to:** F10 (background after initialize, rows older than 60 s, compare-and-set, `ABANDONED` for unsigned, `SETTLEMENT_UNKNOWN` + reconcile for signed), design §3 "Startup recovery (Sugg. 5)", §7 "one case per transition incl. ABANDONED".
**Timebox:** 15 min.
**SDK calls:** none.
**§7 tests covered:** "one case per transition, including ABANDONED".

**Files:**
- Modify: `billguard/recovery.py` (replace the stub)
- Test: `tests/test_recovery.py`

**Implementer brief:** Implement the recovery pass that `server.py` already spawns from the lifespan (after a 2 s sleep, in a background task). It touches only rows whose `updated_at` is older than 60 s. `INTENT_RECORDED`/`QUOTED`/`AUTHORIZED` rows become `ABANDONED` (nothing was signed: the budget is freed, no proven finding). `PAYLOAD_SIGNED`/`SENT` rows become `SETTLEMENT_UNKNOWN` and are reconciled through `purchase.reconcile_and_finish`. Every write is compare-and-set, so a second live process cannot be stomped on.

- [ ] **Step 1: Write the failing tests**

`tests/test_recovery.py`:
```python
import time

from billguard import reconcile as reconcile_mod
from billguard.models import PaymentStatus
from billguard.recovery import STALE_S, run_startup_recovery
from tests.test_guard import seed

P = PaymentStatus


async def test_recovery_abandons_unsigned_and_reconciles_signed(deps, monkeypatch):
    calls = []

    async def fake_reconcile(d, row, deadline):
        calls.append(row.purchase_id)
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    rows = {st: seed(deps.repo, st, intent=f"i-{st}", age_s=STALE_S + 5)
            for st in (P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED, P.PAYLOAD_SIGNED, P.SENT)}
    fresh = seed(deps.repo, P.SENT, intent="fresh")  # updated just now: must be left alone
    terminal = seed(deps.repo, P.SETTLED, intent="done", age_s=STALE_S + 5)

    await run_startup_recovery(deps)

    for st in (P.INTENT_RECORDED, P.QUOTED, P.AUTHORIZED):
        assert deps.repo.get_purchase(rows[st].purchase_id).payment_status == P.ABANDONED
    for st in (P.PAYLOAD_SIGNED, P.SENT):
        got = deps.repo.get_purchase(rows[st].purchase_id)
        assert got.payment_status == P.SETTLEMENT_UNKNOWN and got.history[-1]["by"] == "startup_recovery"
        assert got.purchase_id in calls
        assert any(f.code == "SETTLEMENT_UNKNOWN" for f in deps.repo.findings_for_purchase(got.purchase_id))
    assert deps.repo.get_purchase(fresh.purchase_id).payment_status == P.SENT
    assert deps.repo.get_purchase(terminal.purchase_id).payment_status == P.SETTLED
    # ABANDONED frees the budget; SETTLEMENT_UNKNOWN and SETTLED still count
    assert deps.repo.spent_minor("test") == 500_000 * 4  # two stale signed rows (now unknown), fresh SENT, terminal SETTLED


async def test_recovery_is_idempotent(deps, monkeypatch):
    async def fake_reconcile(d, row, deadline):
        return d.repo.get_purchase(row.purchase_id)

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    row = seed(deps.repo, P.QUOTED, age_s=STALE_S + 5)
    await run_startup_recovery(deps)
    await run_startup_recovery(deps)
    got = deps.repo.get_purchase(row.purchase_id)
    assert got.payment_status == P.ABANDONED and [h["state"] for h in got.history].count("ABANDONED") == 1
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run pytest tests/test_recovery.py -q`
Expected: `ImportError: cannot import name 'STALE_S'`

- [ ] **Step 3: Replace `billguard/recovery.py`**

```python
"""Startup recovery (design §3 Sugg. 5, PRD F10).

Spawned by the FastMCP lifespan as a background task after a 2 s sleep, so it never blocks the
initialize handshake. Touches only rows older than STALE_S; every write is compare-and-set.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from billguard.models import SIGNED_IN_FLIGHT, UNSIGNED_IN_FLIGHT, PaymentStatus, history_entry
from billguard.purchase import CALL_BUDGET_S, reconcile_and_finish

log = logging.getLogger(__name__)
P = PaymentStatus
STALE_S = 60.0


async def run_startup_recovery(deps: Any) -> None:
    repo = deps.repo
    stuck = repo.list_stuck(STALE_S)
    if not stuck:
        log.info("startup recovery: nothing to do")
        return
    for row in stuck:
        st = row.payment_status
        if st in UNSIGNED_IN_FLIGHT:
            if repo.update_status(row.purchase_id, st, P.ABANDONED, history_entry("ABANDONED", by="startup_recovery", was=str(st))):
                log.info("startup recovery: %s %s -> ABANDONED (nothing was signed)", row.purchase_id, st)
        elif st in SIGNED_IN_FLIGHT:
            if repo.update_status(row.purchase_id, st, P.SETTLEMENT_UNKNOWN,
                                  history_entry("SETTLEMENT_UNKNOWN", by="startup_recovery", reason=f"found {st} after restart")):
                log.info("startup recovery: %s %s -> SETTLEMENT_UNKNOWN; reconciling", row.purchase_id, st)
                try:
                    await reconcile_and_finish(deps, repo.get_purchase(row.purchase_id), time.monotonic() + CALL_BUDGET_S)
                except Exception:  # noqa: BLE001
                    log.exception("startup recovery: reconcile failed for %s", row.purchase_id)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest tests/test_recovery.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add billguard/recovery.py tests/test_recovery.py
git commit -m "feat(recovery): abandon unsigned stale rows, reconcile signed ones, compare-and-set only"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_recovery.py -q` → Expected: `passed`, 0 failed
- Run: `grep -c 'STALE_S = 60.0' billguard/recovery.py` → Expected: `1`

**Manual Sepolia check:** kill the MCP server mid-call is not practical; instead, with the seller stopped, start a purchase in Claude Desktop (it will reject `NOT_X402`), then restart Claude Desktop and confirm the stderr log of the server (Claude Desktop → Settings → Developer → logs) shows `startup recovery: nothing to do` within a few seconds of start, and the server answered `list_services` before that line.

### Task 3.3: Schema and vocabulary tests `[W-MANDATE]`

**Maps to:** F2 (no force-retry parameter, no `approve_retry` tool), metric 8, design §6 "None of them has a `force_new_payment` parameter", §7 "schema test".
**Timebox:** 10 min.
**SDK calls:** FastMCP `await mcp.get_tools()` and `Tool.parameters` (JSON schema).
**§7 tests covered:** "a schema test that no paid tool exposes force_new_payment and that no tool named approve_retry is registered".

**Files:**
- Test: `tests/test_schema.py`

**Implementer brief:** Lock the money-safety invariants into tests: every paid tool's JSON schema has `purchase_intent_id` and `task_id` and no parameter whose name contains `force` or `retry`; no tool named `approve_retry` is registered; the `safe_next_actions` vocabulary and the response builder never emit a pay/retry value for any combination of states and findings.

- [ ] **Step 1: Write the tests**

`tests/test_schema.py`:
```python
import itertools
import time

import pytest

from billguard.models import (
    SAFE_NEXT_ACTION_PREFIXES,
    Finding,
    FindingCode,
    FulfillmentStatus,
    PaymentStatus,
    Purchase,
    RejectReason,
)
from billguard.response import safe_next_actions

PAID_TOOLS = ("get_company_report", "get_research_report", "convert_document")
ADMIN_TOOLS = ("list_services", "get_spending_policy", "get_purchase", "list_findings", "reconcile_purchase")


async def test_mcp_schema_has_no_force_retry_and_no_approve_retry():
    from billguard.server import mcp

    tools = await mcp.get_tools()
    names = set(tools)
    assert set(PAID_TOOLS) <= names and set(ADMIN_TOOLS) <= names
    assert "approve_retry" not in names and not any("approve" in n or "force" in n for n in names)
    for n in PAID_TOOLS:
        props = set(tools[n].parameters["properties"])
        assert {"purchase_intent_id", "task_id"} <= props, (n, props)
        assert not any("force" in p.lower() or "retry" in p.lower() for p in props), (n, props)


def test_vocabulary_has_no_pay_or_retry():
    for v in SAFE_NEXT_ACTION_PREFIXES:
        assert "pay" not in v.lower() and "retry" not in v.lower()


@pytest.mark.parametrize("status,fulfillment,codes", list(itertools.product(
    list(PaymentStatus), list(FulfillmentStatus),
    [(), (FindingCode.PAID_NO_DELIVERY,), (FindingCode.FULFILLMENT_FAILED,), (FindingCode.DUPLICATE_SUSPECTED,),
     (FindingCode.SETTLEMENT_UNKNOWN,), (FindingCode.PRICE_MISMATCH, FindingCode.DUPLICATE_SUSPECTED)])))
def test_safe_next_actions_never_pays(status, fulfillment, codes):
    row = Purchase.new(principal_id="p", task_id=None, purchase_intent_id="i", request_fingerprint="f" * 64,
                       service_id="report", args={}, network="eip155:84532")
    row.payment_status, row.fulfillment_status, row.valid_before = status, fulfillment, int(time.time()) + 300
    if status == PaymentStatus.REJECTED:
        row.reject_reason = RejectReason.SERVICE_QUARANTINED
    findings = [Finding("f", row.purchase_id, c, "proven", "s", [], [], time.time()) for c in codes]
    acts = safe_next_actions(row, findings)
    assert acts and all(a.startswith(SAFE_NEXT_ACTION_PREFIXES) for a in acts)
    assert not any("pay" in a or "retry" in a for a in acts)
    if status == PaymentStatus.SETTLEMENT_UNKNOWN:
        assert "reconcile_purchase" in acts
    if status == PaymentStatus.REJECTED:
        assert acts == ["human_review"]
    if FindingCode.DUPLICATE_SUSPECTED in codes and status != PaymentStatus.REJECTED:
        assert acts[:2] == ["human_review", "report_to_seller"]
```

- [ ] **Step 2: Run and commit**

Run: `uv run pytest tests/test_schema.py -q`
Expected: all pass (if `test_mcp_schema...` fails on `get_tools`, use the listing call recorded in Task 2.7 step 3).

```bash
git add tests/test_schema.py
git commit -m "test: lock no-force-retry schema and safe_next_actions vocabulary"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_schema.py -q` → Expected: `passed`, 0 failed

**Manual Sepolia check (M3 checkpoint part 1, metric 3):** in Claude Desktop: *"Buy the premium company report for ACME (tier premium), purchase intent id demo-premium-1"* → `payment_status: REJECTED`, `reject_reason: PRICE_MISMATCH`, a `PRICE_MISMATCH` finding with the quote evidence, no `settlement_tx`, no new transfer on the explorer, and `get_spending_policy` unchanged.

### Milestone M3 — Checkpoint and manual verification

**Checkpoint (§8.3):** a `PRICE_MISMATCH` rejection, and a fresh intent ID gets `DUPLICATE_SUSPECTED` with no payment.

Operator verification list:
- [ ] `uv run pytest -q` green, including `test_guard.py`, `test_recovery.py`, `test_schema.py`.
- [ ] Premium report → `REJECTED/PRICE_MISMATCH`, no transfer (Task 3.3 manual).
- [ ] Convert twice with different intent IDs → one transfer, second result carries `DUPLICATE_SUSPECTED` (Task 3.1 manual).
- [ ] Convert again with the **same** intent ID as the first → same `purchase_id`, `DUPLICATE_SUSPECTED`, `retry_blocked` evidence count grows by one (`get_purchase` shows two `retry_blocked` entries in `history`).
- [ ] `list_findings` shows `DUPLICATE_SUSPECTED` (and `FULFILLMENT_FAILED` or `FULFILLMENT_UNVERIFIED` for the convert, depending on whether M4 has landed).
- [ ] Claude Desktop restart: the server logs the recovery line and still answers tools.
- [ ] `uv run python scripts/reset_demo.py --yes` before moving on.

---

## Milestone M4 — Conversion: PDF artifact, validators, findings `[W-VERIFY]`

**Timebox:** 45 min. **Cut if overrun:** `content_hash` (review S10: `non_empty` is enough for the Bazaar seller); keep `pdf_page_count`. Taking this cut means the controller also changes the Bazaar entry's validators in `billguard/registry.py` to `("non_empty",)` (one-line edit, `[W-CORE]` file), otherwise `resolve()` raises and the Bazaar buy is `FULFILLMENT_UNVERIFIED`.
**Checkpoint (§8.4):** `FULFILLMENT_FAILED` with evidence (page count 12 vs 20).

### Task 4.1: PDF page count, `pdf_page_count` and `content_hash` validators, convert flow `[W-VERIFY]`

**Maps to:** F8 (`pdf_page_count`, `content_hash`; failing validator → `FULFILLMENT_FAILED` referencing evidence rows), metric 5, design §5 validators, §6 artifact (path + page count + hash), §7 "each validator", PRD user story "12 pages against a 20-page contract".
**Timebox:** 30 min.
**SDK calls:** pypdf `PdfReader(path).pages`; no x402 calls.
**§7 tests covered:** "each validator" (pdf_page_count, content_hash) and the scenario-C flow.

**Files:**
- Modify: `billguard/artifacts.py` (fill `pdf_page_count`), `billguard/verify.py` (add two validators)
- Test: `tests/test_artifacts.py`, `tests/test_verify.py` (append)

**Implementer brief:** Fill the `pdf_page_count` stub in `artifacts.py` with pypdf, so every stored PDF artifact carries `page_count`. Add two validators: `pdf_page_count` compares the artifact's page count with `args["pages"]` and fails with a reason naming both numbers; `content_hash` re-hashes the delivered bytes (from the path for file artifacts) and checks they match the recorded `sha256`, recording the hash in the detail so the finding can cite it (for inline JSON artifacts it records the stored hash and passes). Then prove the whole scenario C through `purchase()`: `/convert` returns 12 pages against 20 → `SETTLED`, `fulfillment_status: FAILED`, one `FULFILLMENT_FAILED` finding (proven) whose `evidence_ids` contain the validator row and the artifact row, `safe_next_actions == ["human_review", "report_to_seller"]`, and the response artifact is a path with `page_count: 12`, never base64.

- [ ] **Step 1: Write the failing tests**

`tests/test_artifacts.py`:
```python
from billguard.artifacts import pdf_page_count, store_artifact
from tests.conftest import pdf_bytes


def test_pdf_page_count_from_disk(tmp_path):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p1", tmp_path)
    assert art.kind == "pdf" and art.page_count == 12 and pdf_page_count(art.path) == 12
    assert art.summary()["page_count"] == 12 and "json" not in art.summary()


def test_pdf_page_count_on_garbage_is_none(tmp_path):
    art = store_artifact(b"%PDF-1.4 not really", "application/pdf", "p2", tmp_path)
    assert art.page_count is None
```

Append to `tests/test_verify.py`:
```python


# ---- M4: pdf_page_count and content_hash ----
from billguard.purchase import purchase
from tests.conftest import pdf_bytes


def test_pdf_page_count_validator(reg, tmp_path):
    art = store_artifact(pdf_bytes(12), "application/pdf", "p9", tmp_path)
    results = dict((r[0], r) for r in run_validators(reg["convert"], {"doc_id": "d", "pages": 20}, art))
    name, passed, detail = results["pdf_page_count"]
    assert passed is False and detail["expected"] == 20 and detail["actual"] == 12 and "12" in detail["reason"] and "20" in detail["reason"]
    ok = dict((r[0], r) for r in run_validators(reg["convert"], {"doc_id": "d", "pages": 12}, art))
    assert ok["pdf_page_count"][1] is True


def test_content_hash_validator(tmp_path):
    from dataclasses import replace
    from billguard.verify import content_hash
    svc = replace(load_registry(base_url="http://x")["report"], validators=("content_hash",))
    art = store_artifact(b"hello", "text/plain", "p10", tmp_path)
    name, passed, detail = content_hash(svc, {}, art)
    assert passed and detail["sha256"] == art.sha256 and detail["recomputed"] is True
    Path(art.path).write_bytes(b"tampered")
    assert content_hash(svc, {}, art)[1] is False


async def test_convert_flow_fulfillment_failed_with_evidence(deps, seller):
    r = await purchase(deps, "convert", {"doc_id": "d1", "pages": 20}, "i-convert")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "FAILED"
    assert r["artifact"]["path"].endswith(".pdf") and r["artifact"]["page_count"] == 12 and "json" not in r["artifact"]
    f = [x for x in r["findings"] if x["code"] == "FULFILLMENT_FAILED"]
    assert len(f) == 1 and f[0]["severity"] == "proven" and "12" in f[0]["summary"] and "20" in f[0]["summary"]
    kinds = {e.evidence_id: e.kind for e in deps.repo.list_evidence(r["purchase_id"])}
    assert {kinds[i] for i in f[0]["evidence_ids"]} == {"validator", "artifact"}
    assert r["safe_next_actions"] == ["human_review", "report_to_seller"]
    assert len(seller.paid) == 1 and deps.repo.spent_minor("test") == 2_000_000
```

- [ ] **Step 2: Run and see failures**

Run: `uv run pytest tests/test_artifacts.py tests/test_verify.py -q`
Expected: the new tests fail (`page_count` is `None`, `content_hash` import error, convert flow `UNVERIFIED`).

- [ ] **Step 3: Fill `pdf_page_count` in `billguard/artifacts.py`**

Replace the stub function with:
```python
def pdf_page_count(path: str) -> int | None:
    """Page count via pypdf; None when the file is not a readable PDF (the validator then fails with actual=None)."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception:  # noqa: BLE001
        return None
```

- [ ] **Step 4: Add the validators to `billguard/verify.py`**

Add after `non_empty` and register both in `VALIDATORS`:
```python
def pdf_page_count(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    from billguard import artifacts as _artifacts

    expected = int(args.get("pages", 0))
    actual = art.page_count if art.page_count is not None else (_artifacts.pdf_page_count(art.path) if art.path else None)
    ok = actual is not None and actual == expected
    detail: dict[str, Any] = {"expected": expected, "actual": actual}
    if not ok:
        detail["reason"] = f"page count {actual} != requested {expected}"
    return "pdf_page_count", ok, detail


def content_hash(service: Service, args: dict[str, Any], art: Artifact) -> ValidatorResult:
    import hashlib
    from pathlib import Path

    if art.path:
        actual = hashlib.sha256(Path(art.path).read_bytes()).hexdigest()
        ok = actual == art.sha256
        return "content_hash", ok, {"sha256": art.sha256, "recomputed": True, **({} if ok else {"reason": f"content hash {actual} != recorded {art.sha256}"})}
    return "content_hash", True, {"sha256": art.sha256, "recomputed": False}


VALIDATORS.update({"pdf_page_count": pdf_page_count, "content_hash": content_hash})
```
(and delete the "registered by M4" comment inside the `VALIDATORS` dict.)

- [ ] **Step 5: Run the tests and see them pass; update the M2 convert test expectation**

Run: `uv run pytest tests/test_artifacts.py tests/test_verify.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass. (`tests/test_purchase_flow.py::test_convert_without_pdf_validator_is_unverified_not_failed` accepts `FAILED`.)

- [ ] **Step 6: Commit**

```bash
git add billguard/artifacts.py billguard/verify.py tests/test_artifacts.py tests/test_verify.py
git commit -m "feat(verify): pdf_page_count and content_hash validators; convert yields FULFILLMENT_FAILED with evidence"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_artifacts.py tests/test_verify.py -q` → Expected: `passed`, 0 failed
- Run: `uv run python -c "from billguard.verify import VALIDATORS; print(sorted(VALIDATORS))"` → Expected: `['content_hash', 'content_type', 'non_empty', 'pdf_page_count', 'ticker_matches']`
- Run: `uv run pytest -q` → Expected: all pass

**Manual Sepolia check (M4 checkpoint, metric 5):** in Claude Desktop: *"Convert document contract-7 to a 20-page PDF, purchase intent id demo-convert-9"* → `SETTLED`, `fulfillment_status: FAILED`, a `FULFILLMENT_FAILED` finding whose summary says page count 12 vs 20, `artifact.path` ending in `.pdf` with `page_count: 12`, `safe_next_actions: [human_review, report_to_seller]`. Claude has no tool to pay again. `ls artifacts/` shows the PDF.

### Milestone M4 — Checkpoint and manual verification

- [ ] `uv run pytest -q` green.
- [ ] Convert in Claude Desktop → `FULFILLMENT_FAILED` citing 12 vs 20 (above).
- [ ] `list_findings` lists it with `evidence_ids`; `get_purchase` shows the validator evidence (`expected: 20, actual: 12`) and the artifact evidence (path, sha256).
- [ ] The response contains no base64 (`"json"` absent from the artifact; `path` present).
- [ ] `uv run python scripts/reset_demo.py --yes`.

---

## Milestone M5 — Reconciliation `[W-RECONCILE]`

**Timebox:** 90 min. **Cut if overrun:** the `wait_until` action is already produced by `response.py`, so nothing in the design's cut list applies here. Keep `POLL_ATTEMPTS = 5` and `POLL_INTERVAL_S = 2.0` exactly (PRD F6); if Sepolia RPC lag makes the first poll miss, the single resend and the `reconcile_purchase` tool are the designed recovery, not more polls. If Task 5.4 cannot be finished, run its scenario manually (Task 5.3) and leave the test for M9. Record the backup video at the end of this milestone, before M6 (design §8.5).
**Checkpoint (§8.5):** `SETTLEMENT_UNKNOWN` → chain → `SETTLED` + `PAID_NO_DELIVERY` within one tool call; the retry is refused; `safe_next_actions` contains no payment action.

### Task 5.1: Seller stall hook for `/research-slow` (N1) `[W-RECONCILE]`

**Maps to:** F11 (after-settle async sleep gated on that path only), design §4 research-slow row and N1, §7 "one hook unit test", §10 hook-regression risk.
**Timebox:** 20 min.

**Files:**
- Create: `sellers/stall.py`
- Modify: `sellers/app.py` (register the hook; one import, one line)
- Test: `tests/test_seller_hook.py`

**SDK calls:** `server.on_after_settle(hook)`; the hook receives a `SettleResultContext` whose `.transport_context` is an `HTTPTransportContext` with `.request: HTTPRequestContext` (fields `adapter`, `path`, `method`, ...; no `.url`).
**§7 tests covered:** "one hook unit test: with `HTTPRequestContext(adapter=DummyAdapter(), path="/report", method="GET")` the hook returns at once, and with `/research-slow` it sleeps (patched)".

**Implementer brief:** Scenario B needs the seller to settle on-chain and then stall its response so BillGuard's 15 s client timeout fires. Put the hook in its own module (`sellers/stall.py`) so it can be unit-tested without booting the app (importing `sellers/app.py` contacts the facilitator). The hook is `async`, gates on `ctx.transport_context is not None and ctx.transport_context.request.path == "/research-slow"` (`decoded_path` also acceptable), sleeps `SELLER_STALL_S` (default 90) only then, returns immediately for every other route, and **never raises** (a raising hook turns every settle into a 402). Use `asyncio.sleep`, never `time.sleep`. `sellers/` still imports nothing from `billguard`.

- [ ] **Step 1: Write the failing test**

`tests/test_seller_hook.py`:
```python
from types import SimpleNamespace

from x402.http.types import HTTPRequestContext, HTTPTransportContext

from sellers.stall import make_stall_hook


class DummyAdapter:
    def get_header(self, name):
        return None

    def get_method(self):
        return "GET"

    def get_path(self):
        return "/"

    def get_url(self):
        return "http://seller.test/"

    def get_accept_header(self):
        return "*/*"


def ctx_for(path: str):
    req = HTTPRequestContext(adapter=DummyAdapter(), path=path, method="GET")
    return SimpleNamespace(transport_context=HTTPTransportContext(request=req), result=None)


async def test_hook_sleeps_only_for_research_slow():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    hook = make_stall_hook(stall_s=90, sleep=fake_sleep)
    await hook(ctx_for("/report/ACME"))
    await hook(ctx_for("/convert"))
    await hook(ctx_for("/report-premium/ACME"))
    assert slept == []
    await hook(ctx_for("/research-slow"))
    assert slept == [90]


async def test_hook_never_raises():
    async def boom(_s):
        raise RuntimeError("sleep exploded")

    hook = make_stall_hook(stall_s=1, sleep=boom)
    await hook(ctx_for("/research-slow"))  # swallowed
    await hook(SimpleNamespace(transport_context=None, result=None))  # no transport context: no-op
```

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest tests/test_seller_hook.py -q`
Expected: `ModuleNotFoundError: No module named 'sellers.stall'`

- [ ] **Step 3: Write `sellers/stall.py`**

```python
"""Scenario B after-settle stall for /research-slow (design §4, N1). Imports nothing from billguard.

The hook runs after the facilitator has settled on-chain and the receipt was awaited. Sleeping here
only delays the HTTP response; the transfer is already final. Gated on the HTTPRequestContext path,
so /report, /report-premium and /convert are unaffected. Never raises: a raising after-settle hook
turns the settle into a 402 for every route.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("sellers.stall")
STALL_PATH = "/research-slow"


def make_stall_hook(stall_s: float, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
    async def stall_after_settle(ctx: Any) -> None:
        try:
            tc = getattr(ctx, "transport_context", None)
            req = getattr(tc, "request", None) if tc is not None else None
            path = getattr(req, "path", None)
            if path == STALL_PATH:
                log.info("%s settled; stalling the response for %ss", STALL_PATH, stall_s)
                await sleep(stall_s)
        except Exception:  # noqa: BLE001
            log.exception("stall hook error (ignored so the settle still succeeds)")

    return stall_after_settle
```

- [ ] **Step 4: Register the hook in `sellers/app.py`**

Add the import near the other imports:
```python
from sellers.stall import make_stall_hook
```
and right after `server = build_server()`:
```python
server.on_after_settle(make_stall_hook(float(os.environ.get("SELLER_STALL_S", "90"))))
```

- [ ] **Step 5: Run tests, restart the seller, re-run the smoke (N1 regression check)**

Run: `uv run pytest tests/test_seller_hook.py -q`
Expected: all pass.

Restart: `uv run uvicorn sellers.app:app --port 8402`
Run: `uv run python scripts/smoke_sellers.py`
Expected: `SMOKE OK` and both `/report` and `/convert` still deliver with `settle_header=yes` (design §10: the hook must not break other routes).

- [ ] **Step 6: Commit**

```bash
git add sellers/stall.py sellers/app.py tests/test_seller_hook.py
git commit -m "feat(sellers): after-settle stall hook gated on /research-slow only"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_seller_hook.py -q` → Expected: `passed`, 0 failed
- Run: `grep -rn billguard sellers/ | wc -l | tr -d ' '` → Expected: `0`
- Run: `grep -c 'time.sleep' sellers/stall.py sellers/app.py` → Expected: both `0`
- Run: `grep -c 'on_after_settle' sellers/app.py` → Expected: `1`

**Manual Sepolia check:** `curl` of `/research-slow?topic=x` with a paid header is impractical by hand; the smoke above proves other routes; Task 5.3 proves the stall.

### Task 5.2: Chain reader and reconciliation: poll, single identical resend, block-timestamp failure rule `[W-RECONCILE]`

**Maps to:** F6 (poll `authorizationState` 5 × 2 s, resend identical header at most once per call while valid, `PAYMENT_FAILED` by block timestamp past `validBefore`, never creates a new payment, 45 s budget), F5 (reconciled in the same call), design §5 steps 1–3 and the "resend is safe" notes, §3 `history` entries, §7 "single-resend bound (does not recurse)", review S4, S5, Sugg. 1a/1b/2, metric 4.
**Timebox:** 45 min.

**Files:**
- Create: `billguard/chain.py`
- Modify: `billguard/reconcile.py` (replace the stub)
- Test: `tests/test_reconcile.py`

**SDK calls:** `AUTHORIZATION_STATE_ABI` from `x402.mechanisms.evm.constants`; web3.py `Web3(HTTPProvider(rpc))`, `w3.eth.get_block("latest")`, `contract.functions.authorizationState(from, nonce).call(block_identifier=n)`. Reuses `outcome.send_paid_request` and `outcome.classify_outcome` for the resend.
**§7 tests covered:** "the single-resend bound (the resend outcome does not recurse)", "each step 6 outcome branch" for the resend classification, `attempt+1 after PAYMENT_FAILED` precondition (PAYMENT_FAILED is produced here).

**Implementer brief:** Implement design §5 for a `SETTLEMENT_UNKNOWN` row. Read `from`, `nonce`, `validBefore` and the encoded header from the `payment_payload` evidence. Poll `authorizationState(from, nonce)` up to 5 times, 2 s apart, stopping at the first `true` and respecting the call deadline; read the block number and **block timestamp at the same block** (S5). `true` → `SETTLED` with a `chain_check` evidence row and `history` entry (no tx hash; the response shows the block). `false` with block timestamp beyond `validBefore + 30 s` → `PAYMENT_FAILED` (proven finding). `false` and still valid → **one** resend of the identical stored header with an 8 s timeout, recorded as `resend` evidence and classified by `classify_outcome` (never re-entering reconcile); if that did not settle, **one more** poll, then stop. The `PAYMENT-REQUIRED.error` text from a 402 (for example `invalid_exact_evm_nonce_already_used`) is recorded but never decides money. The chain reader is injectable through `deps.chain_reader` so tests use fakes; the default is `chain.authorization_state`, which runs the blocking web3 call in a thread.

- [ ] **Step 1: Write the failing tests**

`tests/test_reconcile.py`:
```python
import time

from billguard.chain import ChainState
from billguard.purchase import purchase
from billguard.reconcile import reconcile
from tests.conftest import FakeSeller


def chain_seq(*states):
    """Fake deps.chain_reader: returns states in order, repeating the last one. states: (used, block, ts)."""
    calls = []

    async def reader(network, from_addr, nonce):
        calls.append((network, from_addr, nonce))
        used, bn, ts = states[min(len(calls) - 1, len(states) - 1)]
        return ChainState(used=used, block_number=bn, block_timestamp=ts)

    reader.calls = calls
    return reader


def hist(deps, pid):
    return [h["state"] for h in deps.repo.get_purchase(pid).history]


def codes(deps, pid):
    return [(f.code, f.resolved_at is None) for f in deps.repo.findings_for_purchase(pid)]


async def test_chain_true_settles_and_no_artifact_is_paid_no_delivery(make_deps):
    seller = FakeSeller(mode="timeout")
    reader = chain_seq((True, 123, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "agents"}, "i1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "NO_DELIVERY"
    assert [c for c, _ in codes(deps, r["purchase_id"])] == ["PAID_NO_DELIVERY"]  # SETTLEMENT_UNKNOWN never created: resolved in-call
    assert hist(deps, r["purchase_id"])[-5:] == ["SENT", "SETTLEMENT_UNKNOWN", "chain_check", "SETTLED", "NO_DELIVERY"]
    assert len(seller.paid) == 1 and len(reader.calls) == 1
    assert r["safe_next_actions"] == ["human_review", "report_to_seller"]
    ev = {e.kind for e in deps.repo.list_evidence(r["purchase_id"])}
    assert "chain_check" in ev and "resend" not in ev


async def test_expired_by_block_timestamp_is_payment_failed(make_deps):
    seller = FakeSeller(mode="timeout")
    far_future = int(time.time()) + 10_000  # chain time says the authorization window is long over
    deps = make_deps(seller, chain_reader=chain_seq((False, 5, far_future)))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "PAYMENT_FAILED"
    assert codes(deps, r["purchase_id"]) == [("PAYMENT_FAILED", True)]
    assert len(seller.paid) == 1  # no resend after a proven expiry
    assert deps.repo.spent_minor("test") == 0  # PAYMENT_FAILED frees the budget
    assert r["safe_next_actions"] == ["get_purchase"]


async def test_still_valid_resends_identical_header_once_and_settles(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "ok"]  # first paid request stalls, the resend delivers
    deps = make_deps(seller, chain_reader=chain_seq((False, 7, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "VERIFIED"
    assert len(seller.paid) == 2
    assert seller.paid[0].headers["PAYMENT-SIGNATURE"] == seller.paid[1].headers["PAYMENT-SIGNATURE"]  # identical header
    kinds = [e.kind for e in deps.repo.list_evidence(r["purchase_id"])]
    assert kinds.count("resend") == 1 and "settlement" in kinds


async def test_resend_nonce_used_triggers_one_more_poll_then_settles(make_deps):
    seller = FakeSeller(mode="timeout")
    seller.paid_modes = ["timeout", "nonce_used"]
    reader = chain_seq((False, 1, int(time.time())), (False, 1, int(time.time())), (False, 1, int(time.time())),
                       (False, 1, int(time.time())), (False, 1, int(time.time())), (True, 9, int(time.time())))
    deps = make_deps(seller, chain_reader=reader)
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLED" and r["fulfillment_status"] == "NO_DELIVERY"
    assert len(seller.paid) == 2  # exactly one resend
    resend = [e for e in deps.repo.list_evidence(r["purchase_id"]) if e.kind == "resend"][0]
    assert resend.payload["payment_required_error"] == "invalid_exact_evm_nonce_already_used"
    assert len(reader.calls) == 6  # 5 polls, then 1 re-poll that returned true


async def test_resend_bound_is_one_per_call_and_row_stays_unknown(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq((False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    assert r["payment_status"] == "SETTLEMENT_UNKNOWN" and r["valid_before"] is not None
    assert len(seller.paid) == 2  # original + one resend, never more
    assert codes(deps, r["purchase_id"]) == [("SETTLEMENT_UNKNOWN", True)]
    assert "reconcile_purchase" in r["safe_next_actions"]
    # a later reconcile call gets its own single resend
    row = deps.repo.get_purchase(r["purchase_id"])
    await reconcile(deps, row, time.monotonic() + 45)
    assert len(seller.paid) == 3


async def test_deadline_exhausted_means_no_resend(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=chain_seq((False, 1, int(time.time()))))
    r = await purchase(deps, "research", {"topic": "x"}, "i1")
    row = deps.repo.get_purchase(r["purchase_id"])
    before = len(seller.paid)
    got = await reconcile(deps, row, time.monotonic() - 1)  # already past the budget
    assert got.payment_status == "SETTLEMENT_UNKNOWN" and len(seller.paid) == before


async def test_reconcile_is_a_noop_on_non_unknown_rows(deps, seller):
    r = await purchase(deps, "report", {"ticker": "A"}, "i1")
    row = deps.repo.get_purchase(r["purchase_id"])
    got = await reconcile(deps, row, time.monotonic() + 45)
    assert got.payment_status == "SETTLED" and len(seller.paid) == 1
```

- [ ] **Step 2: Run and see failures**

Run: `uv run pytest tests/test_reconcile.py -q`
Expected: `ModuleNotFoundError: No module named 'billguard.chain'`

- [ ] **Step 3: Write `billguard/chain.py`**

```python
"""EIP-3009 authorizationState reads on the USDC contract (design §5). Chain time, not local time (S5)."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import lru_cache

from web3 import Web3
from x402.mechanisms.evm.constants import AUTHORIZATION_STATE_ABI

from billguard.registry import MAINNET, SEPOLIA, USDC

RPC_ENV = {SEPOLIA: ("RPC_URL_SEPOLIA", "https://sepolia.base.org"), MAINNET: ("RPC_URL_MAINNET", "https://mainnet.base.org")}


@dataclass(frozen=True)
class ChainState:
    used: bool
    block_number: int
    block_timestamp: int


@lru_cache(maxsize=4)
def _w3(network: str) -> Web3:
    env, default = RPC_ENV[network]
    return Web3(Web3.HTTPProvider(os.environ.get(env, default), request_kwargs={"timeout": 10}))


def _read(network: str, from_addr: str, nonce: str) -> ChainState:
    w3 = _w3(network)
    block = w3.eth.get_block("latest")
    contract = w3.eth.contract(address=Web3.to_checksum_address(USDC[network]), abi=AUTHORIZATION_STATE_ABI)
    nonce_bytes = bytes.fromhex(nonce[2:] if nonce.startswith("0x") else nonce)
    used = contract.functions.authorizationState(Web3.to_checksum_address(from_addr), nonce_bytes).call(
        block_identifier=block["number"])
    return ChainState(used=bool(used), block_number=int(block["number"]), block_timestamp=int(block["timestamp"]))


async def authorization_state(network: str, from_addr: str, nonce: str) -> ChainState:
    """Non-blocking wrapper: web3's HTTP provider is synchronous."""
    return await asyncio.to_thread(_read, network, from_addr, nonce)
```

- [ ] **Step 4: Replace `billguard/reconcile.py`**

```python
"""Design §5 reconciliation for SETTLEMENT_UNKNOWN rows. Never creates a new payment.

Order per call: poll authorizationState (≤5 × 2 s) → true: SETTLED; false past validBefore (+30 s, by
block timestamp): PAYMENT_FAILED; false and valid: ONE resend of the identical stored header, classified
by the step-6 rules without re-entering reconcile → if still unknown, ONE more poll → stop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from billguard import chain
from billguard.chain import ChainState
from billguard.models import EvidenceKind, FindingCode, NextAction, PaymentStatus, Purchase, history_entry
from billguard.outcome import classify_outcome, send_paid_request

log = logging.getLogger(__name__)
P = PaymentStatus
POLL_ATTEMPTS = 5
POLL_INTERVAL_S = 2.0
EXPIRY_MARGIN_S = 30
RESEND_TIMEOUT_S = 8.0  # review S4: keeps the worst case under the 45 s call budget


def _payment_payload(repo: Any, purchase_id: str) -> dict[str, Any] | None:
    rows = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.PAYMENT_PAYLOAD]
    return rows[-1].payload if rows else None


async def _poll(deps: Any, reader: Any, row: Purchase, network: str, from_addr: str, nonce: str, deadline: float) -> ChainState | None:
    last: ChainState | None = None
    polls = 0
    for i in range(POLL_ATTEMPTS):
        if time.monotonic() >= deadline:
            break
        try:  # review S4: an RPC read never runs past the call budget
            last = await asyncio.wait_for(reader(network, from_addr, nonce), timeout=max(1.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            log.warning("chain read timed out for %s", row.purchase_id)
            break
        polls += 1
        if last.used:
            break
        if i < POLL_ATTEMPTS - 1:
            await deps.sleep(POLL_INTERVAL_S)
    if last is not None:
        deps.repo.add_evidence(row.purchase_id, EvidenceKind.CHAIN_CHECK, {
            "used": last.used, "block_number": last.block_number, "block_timestamp": last.block_timestamp,
            "polls": polls, "from": from_addr, "nonce": nonce, "network": network})
        deps.repo.append_history(row.purchase_id, history_entry("chain_check", used=last.used, block=last.block_number, polls=polls))
    return last


def _settle_from_chain(repo: Any, row: Purchase, state: ChainState) -> Purchase:
    repo.add_evidence(row.purchase_id, EvidenceKind.SETTLEMENT, {"source": "authorizationState", "block_number": state.block_number,
                                                                 "block_timestamp": state.block_timestamp, "network": row.network})
    repo.update_status(row.purchase_id, P.SETTLEMENT_UNKNOWN, P.SETTLED, history_entry("SETTLED", via="chain_check", block=state.block_number))
    repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
    return repo.get_purchase(row.purchase_id)


def _fail(repo: Any, row: Purchase, state: ChainState, valid_before: int) -> Purchase:
    chain_ev = [e.evidence_id for e in repo.list_evidence(row.purchase_id) if e.kind == EvidenceKind.CHAIN_CHECK][-1:]
    repo.update_status(row.purchase_id, P.SETTLEMENT_UNKNOWN, P.PAYMENT_FAILED,
                       history_entry("PAYMENT_FAILED", block_timestamp=state.block_timestamp, valid_before=valid_before))
    repo.add_finding(row.purchase_id, FindingCode.PAYMENT_FAILED, "proven",
                     f"authorizationState is false and block time {state.block_timestamp} is past validBefore {valid_before} "
                     f"(+{EXPIRY_MARGIN_S}s): the authorization can never land", chain_ev, [NextAction.GET_PURCHASE])
    repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
    return repo.get_purchase(row.purchase_id)


async def reconcile(deps: Any, row: Purchase, deadline: float) -> Purchase:
    repo = deps.repo
    row = repo.get_purchase(row.purchase_id) or row
    if row.payment_status != P.SETTLEMENT_UNKNOWN:
        return row
    pp = _payment_payload(repo, row.purchase_id)
    if pp is None:
        log.error("reconcile %s: no payment_payload evidence; leaving for human review", row.purchase_id)
        return row
    service = deps.registry[row.service_id]
    reader = deps.chain_reader or chain.authorization_state
    valid_before = int(pp["validBefore"])

    # 1-2. poll the chain first
    state = await _poll(deps, reader, row, service.network, pp["from"], pp["nonce"], deadline)
    if state is None:
        return row
    if state.used:
        return _settle_from_chain(repo, row, state)
    if state.block_timestamp > valid_before + EXPIRY_MARGIN_S:
        return _fail(repo, row, state, valid_before)
    if time.monotonic() + RESEND_TIMEOUT_S >= deadline:
        repo.append_history(row.purchase_id, history_entry("still_unknown", reason="call budget exhausted before resend", valid_before=valid_before))
        return row

    # 3. still valid: one resend of the identical stored header (Sugg. 1a)
    method, url, body = service.build_request(row.args)
    async with deps.http_factory(service) as http:
        outcome = await send_paid_request(http, method, url, body, pp["header"], min(RESEND_TIMEOUT_S, service.timeout_s))
    repo.add_evidence(row.purchase_id, EvidenceKind.RESEND, {"status": outcome.status, "error": outcome.error,
                                                            "payment_required_error": outcome.payment_required_error()})
    row = classify_outcome(repo, row, outcome, P.SETTLEMENT_UNKNOWN, deps.artifact_dir, via="resend")
    if row.payment_status == P.SETTLED:
        repo.resolve_findings(row.purchase_id, FindingCode.SETTLEMENT_UNKNOWN)
        return row

    # one more chain poll, no further resend (Sugg. 1b)
    state = await _poll(deps, reader, row, service.network, pp["from"], pp["nonce"], deadline)
    if state is not None and state.used:
        return _settle_from_chain(repo, row, state)
    if state is not None and state.block_timestamp > valid_before + EXPIRY_MARGIN_S:
        return _fail(repo, row, state, valid_before)
    repo.append_history(row.purchase_id, history_entry("still_unknown", valid_before=valid_before))
    return repo.get_purchase(row.purchase_id)
```

- [ ] **Step 5: Run the tests and see them pass**

Run: `uv run pytest tests/test_reconcile.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass. `tests/conftest.py` defaults `chain_reader` to `never_used` (authorization never seen, block time 0), so the real web3 reader is never called under pytest. With that reader the row is "still valid", so the real reconcile resends the identical header once; the M2/M3 flow tests were written to assert "one signed authorization", not a paid-request count, for exactly this reason.

- [ ] **Step 6: Commit**

```bash
git add billguard/chain.py billguard/reconcile.py tests/test_reconcile.py
git commit -m "feat(reconcile): authorizationState poll, single identical resend, block-timestamp PAYMENT_FAILED"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_reconcile.py -q` → Expected: `passed`, 0 failed
- Run: `uv run pytest -q` → Expected: all pass, no network access (run with Wi-Fi off once to prove it)
- Run: `grep -c 'send_paid_request(' billguard/reconcile.py` → Expected: `1` (exactly one call; the import line has no parenthesis)
- Run: `grep -c 'time.time()' billguard/reconcile.py` → Expected: `0` (chain time decides, not the local clock)
- Run: `grep -c 'create_payment_payload' billguard/reconcile.py` → Expected: `0` (never a new payment)

**Manual Sepolia check:** Task 5.3.

### Task 5.3: Scenario B on Sepolia, retry refused, backup video `[W-RECONCILE]`

**Maps to:** metric 4, design §8.5 checkpoint and "Record the backup video now", §9 demo step 5, §10 facilitator-flakiness fallback (the video).
**Timebox:** 25 min.
**SDK calls:** none (operator task through Claude Desktop).
**§7 tests covered:** the manual "one Sepolia run per scenario" for scenario B.

**Files:**
- Create: `docs/billguard/demo/backup-video.md` (where the recording lives and what it shows)

**Implementer brief (operator task):** Prove the full scenario B end to end on Sepolia through Claude Desktop, then record the screen for the fallback.

- [ ] **Step 1: Reset and run scenario B**

Run: `uv run python scripts/reset_demo.py --yes`. Seller running with the stall hook (`SELLER_STALL_S=90`).
In Claude Desktop: *"Buy a research report on 'agent payments', purchase intent id demo-research-1."*
Expected within one tool call (about 17–30 s): `payment_status: SETTLED`, `fulfillment_status: NO_DELIVERY`, `history` containing `SENT` → `SETTLEMENT_UNKNOWN` (reason `ReadTimeout`) → `chain_check` (`used: true`, block N) → `SETTLED` → `NO_DELIVERY`; findings `PAID_NO_DELIVERY` (proven); `safe_next_actions: [human_review, report_to_seller]`.

- [ ] **Step 2: Presenter line: "Try that research purchase again."**

Expected: either the same intent ID → same `purchase_id`, `DUPLICATE_SUSPECTED` (S1), or a reworded topic → `REJECTED/SERVICE_QUARANTINED` with `safe_next_actions: [human_review]` (S3). Explorer shows exactly one 0.75 USDC transfer.

- [ ] **Step 3: Record the backup video**

Record steps 1–2 plus one report purchase and one convert purchase (QuickTime screen recording). Save outside the repo (for example `~/Movies/billguard-backup.mov`) and write its path and timestamps into `docs/billguard/demo/backup-video.md`:
```markdown
# Backup video

Path: ~/Movies/billguard-backup.mov (recorded <date time>, after M5)
Timestamps: 0:00 smoke_sellers, 0:40 report VERIFIED, 1:20 research SETTLEMENT_UNKNOWN→SETTLED+PAID_NO_DELIVERY, 2:10 "try again" DUPLICATE_SUSPECTED, 2:40 convert FULFILLMENT_FAILED, 3:10 list_findings
Play when: facilitator or RPC is down during the live demo (design §10).
```

- [ ] **Step 4: Commit**

```bash
git add docs/billguard/demo/backup-video.md
git commit -m "docs(demo): backup video location and timestamps"
```

**Exit criteria (auto):**
- Run: `grep -c '^Path:' docs/billguard/demo/backup-video.md` → Expected: `1`
- Run: `test -f "$(grep '^Path:' docs/billguard/demo/backup-video.md | sed -E 's/^Path: *([^ ]+).*/\1/' | sed "s#^~#$HOME#")" && echo ok` → Expected: `ok` (the recording exists on disk)

### Task 5.4: End-to-end scenario B test (merge gate for M3 + M5) `[W-RECONCILE]`

**Maps to:** metric 4 (all three "try again" variants and exactly one transfer), design §8.5 checkpoint "the retry is refused", §7 "duplicate beat in both variants" and "SERVICE_QUARANTINED after an unresolved PAID_NO_DELIVERY", PRD non-functional "every finding references at least one evidence row".
**Timebox:** 10 min. **Run only after both M3 (guard) and M5 (reconcile) are merged**; before that the test is expected to fail and that is the point: it is the gate that proves the two parallel tracks compose.
**SDK calls:** none new. **§7 tests covered:** the end-to-end form of the M3 guard tests under the real reconcile.

**Files:**
- Test: `tests/test_scenario_b.py`

**Implementer brief:** One test that walks the demo's scenario B through `purchase()` with the fake seller stalling and a fake chain reader that says the authorization was used: the first call ends `SETTLED` + `NO_DELIVERY`; "try again" with the same intent, with a new intent and the same topic, and with a reworded topic each refuse without a second signed authorization; every finding has evidence.

- [ ] **Step 1: Write the test**

```python
import time

from billguard.chain import ChainState
from billguard.purchase import purchase
from tests.conftest import FakeSeller


async def used_on_chain(_network, _from, _nonce):
    return ChainState(used=True, block_number=42, block_timestamp=int(time.time()))


async def test_scenario_b_end_to_end(make_deps):
    seller = FakeSeller(mode="timeout")
    deps = make_deps(seller, chain_reader=used_on_chain)

    first = await purchase(deps, "research", {"topic": "agent payments"}, "demo-research-1")
    assert first["payment_status"] == "SETTLED" and first["fulfillment_status"] == "NO_DELIVERY"
    assert [f["code"] for f in first["findings"]] == ["PAID_NO_DELIVERY"]
    assert first["safe_next_actions"] == ["human_review", "report_to_seller"]

    same = await purchase(deps, "research", {"topic": "agent payments"}, "demo-research-1")
    assert same["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in same["findings"]]

    new_intent = await purchase(deps, "research", {"topic": "agent payments"}, "demo-research-2")
    assert new_intent["purchase_id"] == first["purchase_id"] and "DUPLICATE_SUSPECTED" in [f["code"] for f in new_intent["findings"]]

    reworded = await purchase(deps, "research", {"topic": "payments by agents"}, "demo-research-3")
    assert reworded["payment_status"] == "REJECTED" and reworded["reject_reason"] == "SERVICE_QUARANTINED"
    assert reworded["safe_next_actions"] == ["human_review"]

    assert len({q.headers["PAYMENT-SIGNATURE"] for q in seller.paid}) == 1  # exactly one authorization ever signed
    for resp in (first, same, new_intent, reworded):
        for f in resp["findings"]:
            assert f["evidence_ids"], f  # every finding references evidence
        assert not any("pay" in a or "retry" in a for a in resp["safe_next_actions"])

    listed = deps.repo.list_findings("test", unresolved_only=True)
    assert {f["code"] for f in listed} == {"PAID_NO_DELIVERY", "DUPLICATE_SUSPECTED", "SERVICE_QUARANTINED"}
```

- [ ] **Step 2: Run and commit**

Run: `uv run pytest tests/test_scenario_b.py -q`
Expected: 1 passed (only once M3 and M5 are both in the tree).

```bash
git add tests/test_scenario_b.py
git commit -m "test: scenario B end to end across guard and reconcile"
```

**Exit criteria (auto):**
- Run: `uv run pytest tests/test_scenario_b.py -q` → Expected: `1 passed`
- Run: `uv run pytest -q` → Expected: all pass

**Manual Sepolia check:** Task 5.3 is the live form of this test.

### Milestone M5 — Checkpoint and manual verification

**Checkpoint (§8.5):** `SETTLEMENT_UNKNOWN` → chain → `SETTLED` + `PAID_NO_DELIVERY`; retry refused; no payment action in `safe_next_actions`. Backup video recorded.

- [ ] `uv run pytest -q` green with Wi-Fi off (no test touches the network).
- [ ] `uv run pytest tests/test_scenario_b.py -q` passes once M3 and M5 are both merged (the parallel-track merge gate).
- [ ] Scenario B in Claude Desktop matches Task 5.3 step 1 (one tool call, under 45 s).
- [ ] "Try again" (same intent) → `DUPLICATE_SUSPECTED`; "reworded topic" → `SERVICE_QUARANTINED`; explorer shows one 0.75 transfer.
- [ ] `reconcile_purchase` on the settled row returns it unchanged and makes no request to the seller (seller log shows nothing).
- [ ] `smoke_sellers.py` still prints `SMOKE OK` (hook regression, §10).
- [ ] Backup video recorded and its path documented.
- [ ] Note the wall-clock time M5 landed: **after 03:00 → M6 moves to stretch (M8)**.
- [ ] `uv run python scripts/reset_demo.py --yes`.

---

## Milestone M6 — Supabase flip `[W-SUPABASE]`

**Timebox:** 45 min, hard. **Cut rule (§8.6, S10):** if M5 landed after 03:00, skip M6 entirely (it becomes M8.3). If the 45 min elapse without the step-5 checkpoint passing on `BILLGUARD_STORE=supabase`, set it back to `sqlite` and move on; nothing else changes.
**Checkpoint (§8.6):** the step-5 checkpoint (scenario B) passes with `BILLGUARD_STORE=supabase`.

### Task 6.1: `store/supabase.py`, schema, env-gated tests `[W-SUPABASE]`

**Maps to:** F13 (Supabase backend behind the Repository protocol, selected by `BILLGUARD_STORE`), design §1 "flip to Supabase once steps 1–5 pass", §3 "supabase-py upsert(ignore_duplicates=True) returns empty data on conflict, so it is followed by a select", §2 "supabase-py is synchronous; blocking calls are acceptable".
**Timebox:** 45 min.
**SDK calls:** supabase-py `create_client(url, key)`, `table().upsert(..., on_conflict=..., ignore_duplicates=True)`, `.update().eq().eq()`, `.in_()`, `.is_('resolved_at','null')`; `postgrest.exceptions.APIError`.
**§7 tests covered:** the store suite re-run against Supabase (env-gated).

**Files:**
- Create: `billguard/store/supabase.py`, `supabase/schema.sql`
- Modify: `billguard/store/__init__.py` (already has the `supabase` branch; only confirm the import name)
- Test: `tests/test_store_supabase.py` (skipped unless `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set)

**Implementer brief:** Implement the same `Repository` protocol over supabase-py (synchronous client; blocking in async handlers is accepted for the demo). Compare-and-set is `update(...).eq("purchase_id", id).eq("payment_status", expected)` and succeeds iff exactly one row comes back. History append is read-modify-write under the same CAS predicate. `create_purchase` uses `upsert(..., ignore_duplicates=True)` followed by a select: empty data means the key already existed → `DuplicateKeyError`. Amounts are integers; `args`, `history`, `evidence_ids`, `safe_next_actions` and evidence `payload` are `jsonb`. Keep logging to stderr (supabase-py logs are already routed by `configure_logging`).

- [ ] **Step 1: Write `supabase/schema.sql` and apply it in the Supabase SQL editor**

```sql
create table if not exists purchases (
  purchase_id text primary key,
  principal_id text not null,
  task_id text,
  purchase_intent_id text not null,
  request_fingerprint text not null,
  attempt integer not null,
  supersedes_purchase_id text,
  approved_by text,
  service_id text not null,
  args jsonb not null,
  quoted_amount_minor bigint,
  authorized_amount_minor bigint,
  asset text,
  network text not null,
  pay_to text,
  payment_status text not null,
  fulfillment_status text not null,
  settlement_tx text,
  valid_before bigint,
  artifact_hash text,
  reject_reason text,
  history jsonb not null default '[]'::jsonb,
  created_at double precision not null,
  updated_at double precision not null,
  unique (principal_id, purchase_intent_id, request_fingerprint, attempt)
);
create index if not exists idx_purchases_fp on purchases (principal_id, request_fingerprint, created_at);
create table if not exists evidence (
  evidence_id text primary key,
  purchase_id text not null references purchases (purchase_id) on delete cascade,
  kind text not null,
  payload jsonb not null,
  created_at double precision not null
);
create index if not exists idx_evidence_purchase on evidence (purchase_id, created_at);
create table if not exists findings (
  finding_id text primary key,
  purchase_id text not null references purchases (purchase_id) on delete cascade,
  code text not null,
  severity text not null,
  summary text not null,
  evidence_ids jsonb not null default '[]'::jsonb,
  safe_next_actions jsonb not null default '[]'::jsonb,
  created_at double precision not null,
  resolved_at double precision
);
create index if not exists idx_findings_purchase on findings (purchase_id);
```

- [ ] **Step 2: Write `tests/test_store_supabase.py`**

```python
"""Runs only when SUPABASE_URL and SUPABASE_SERVICE_KEY are set. Uses a throwaway principal id per run."""
import os
import time
import uuid

import pytest

from billguard.models import FulfillmentStatus, PaymentStatus, Purchase, history_entry
from billguard.store.base import DuplicateKeyError

pytestmark = pytest.mark.skipif(not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")),
                                reason="SUPABASE_URL/SUPABASE_SERVICE_KEY not set")


@pytest.fixture
def repo():
    from billguard.store.supabase import SupabaseRepository

    return SupabaseRepository.from_env()


@pytest.fixture
def principal(repo):
    pid = f"test-{uuid.uuid4().hex[:8]}"
    yield pid
    repo.reset(pid)


def mk(principal, intent="i1", attempt=1):
    return Purchase.new(principal_id=principal, task_id=None, purchase_intent_id=intent, request_fingerprint="f" * 64,
                        service_id="report", args={"ticker": "ACME"}, network="eip155:84532", attempt=attempt)


def test_create_cas_spent_findings_reset(repo, principal):
    row = repo.create_purchase(mk(principal))
    with pytest.raises(DuplicateKeyError):
        repo.create_purchase(mk(principal))
    assert repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED,
                              history_entry("QUOTED"), quoted_amount_minor=500000)
    assert not repo.update_status(row.purchase_id, PaymentStatus.INTENT_RECORDED, PaymentStatus.QUOTED)
    assert repo.update_status(row.purchase_id, PaymentStatus.QUOTED, PaymentStatus.AUTHORIZED, authorized_amount_minor=500000)
    got = repo.get_purchase(row.purchase_id)
    assert got.payment_status == PaymentStatus.AUTHORIZED and [h["state"] for h in got.history] == ["INTENT_RECORDED", "QUOTED"]
    assert repo.spent_minor(principal) == 500000
    e = repo.add_evidence(row.purchase_id, "quote", {"amount": "500000"})
    repo.add_finding(row.purchase_id, "PRICE_MISMATCH", "proven", "x", [e], ["get_purchase"])
    assert repo.has_unresolved_service_finding(principal, "report", "PRICE_MISMATCH")
    assert repo.list_findings(principal)[0]["service_id"] == "report"
    assert repo.resolve_findings(row.purchase_id, "PRICE_MISMATCH") == 1
    repo.set_fulfillment(row.purchase_id, FulfillmentStatus.VERIFIED, history_entry("VERIFIED"))
    assert repo.get_purchase(row.purchase_id).fulfillment_status == FulfillmentStatus.VERIFIED
    assert [r.attempt for r in repo.find_by_key(principal, "i1", "f" * 64)] == [1]
    assert repo.find_recent_by_fingerprint(principal, "f" * 64, time.time() - 60)[0].purchase_id == row.purchase_id
    assert repo.reset(principal) == {"purchases": 1, "evidence": 1, "findings": 1}
```

- [ ] **Step 3: Write `billguard/store/supabase.py`**

```python
"""Supabase repository (design §1: flipped on after step 5). supabase-py is synchronous; acceptable for the demo."""
from __future__ import annotations

import os
from typing import Any

from postgrest.exceptions import APIError
from supabase import Client, create_client

from billguard.models import (
    BUDGET_STATES,
    IN_FLIGHT,
    Evidence,
    Finding,
    FulfillmentStatus,
    PaymentStatus,
    Purchase,
    assert_transition,
    new_id,
    now,
)
from billguard.store.base import DuplicateKeyError

PROTECTED_COLS = {"purchase_id", "payment_status", "history", "principal_id"}


def _row(d: dict[str, Any]) -> Purchase:
    d = dict(d)
    d["payment_status"] = PaymentStatus(d["payment_status"])
    d["fulfillment_status"] = FulfillmentStatus(d["fulfillment_status"])
    return Purchase(**{k: d.get(k) for k in Purchase.__dataclass_fields__})


def _finding(d: dict[str, Any]) -> Finding:
    return Finding(finding_id=d["finding_id"], purchase_id=d["purchase_id"], code=d["code"], severity=d["severity"],
                   summary=d["summary"], evidence_ids=list(d.get("evidence_ids") or []),
                   safe_next_actions=list(d.get("safe_next_actions") or []), created_at=d["created_at"], resolved_at=d.get("resolved_at"))


class SupabaseRepository:
    def __init__(self, client: Client) -> None:
        self.c = client

    @classmethod
    def from_env(cls) -> "SupabaseRepository":
        return cls(create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]))

    # ---- purchases -------------------------------------------------------
    def create_purchase(self, row: Purchase) -> Purchase:
        d = row.to_dict()
        try:
            res = self.c.table("purchases").upsert(
                d, on_conflict="principal_id,purchase_intent_id,request_fingerprint,attempt", ignore_duplicates=True).execute()
        except APIError as e:  # belt and braces: a 23505 unique violation is also a duplicate
            if getattr(e, "code", None) == "23505" or "23505" in str(e):
                raise DuplicateKeyError(str(e)) from e
            raise
        if not res.data:  # conflict on the unique key returns empty data (design §3)
            raise DuplicateKeyError(f"{row.principal_id}/{row.purchase_intent_id}/{row.request_fingerprint}/{row.attempt}")
        return self.get_purchase(row.purchase_id)  # type: ignore[return-value]

    def get_purchase(self, purchase_id: str) -> Purchase | None:
        res = self.c.table("purchases").select("*").eq("purchase_id", purchase_id).limit(1).execute()
        return _row(res.data[0]) if res.data else None

    def find_by_key(self, principal_id: str, purchase_intent_id: str, request_fingerprint: str) -> list[Purchase]:
        res = (self.c.table("purchases").select("*").eq("principal_id", principal_id).eq("purchase_intent_id", purchase_intent_id)
               .eq("request_fingerprint", request_fingerprint).order("attempt").execute())
        return [_row(d) for d in res.data]

    def find_recent_by_fingerprint(self, principal_id: str, request_fingerprint: str, since_ts: float) -> list[Purchase]:
        res = (self.c.table("purchases").select("*").eq("principal_id", principal_id).eq("request_fingerprint", request_fingerprint)
               .gte("created_at", since_ts).order("created_at", desc=True).execute())
        return [_row(d) for d in res.data]

    def update_status(self, purchase_id: str, expected: PaymentStatus, new: PaymentStatus,
                      history_entry: dict[str, Any] | None = None, **fields: Any) -> bool:
        expected, new = PaymentStatus(expected), PaymentStatus(new)
        assert_transition(expected, new)
        bad = [k for k in fields if k in PROTECTED_COLS]
        if bad:
            raise ValueError(f"cannot set columns {bad} through update_status")
        patch: dict[str, Any] = {"payment_status": str(new), "updated_at": now(), **{k: (str(v) if isinstance(v, (PaymentStatus, FulfillmentStatus)) else v) for k, v in fields.items()}}
        if history_entry is not None:
            cur = self.get_purchase(purchase_id)
            if cur is None or cur.payment_status != expected:
                return False
            patch["history"] = [*cur.history, history_entry]
        res = self.c.table("purchases").update(patch).eq("purchase_id", purchase_id).eq("payment_status", str(expected)).execute()
        return len(res.data) == 1

    def set_fulfillment(self, purchase_id: str, status: FulfillmentStatus,
                        history_entry: dict[str, Any] | None = None, **fields: Any) -> None:
        patch: dict[str, Any] = {"fulfillment_status": str(FulfillmentStatus(status)), "updated_at": now(), **fields}
        if history_entry is not None:
            cur = self.get_purchase(purchase_id)
            patch["history"] = [*(cur.history if cur else []), history_entry]
        self.c.table("purchases").update(patch).eq("purchase_id", purchase_id).execute()

    def append_history(self, purchase_id: str, entry: dict[str, Any]) -> None:
        cur = self.get_purchase(purchase_id)
        if cur is None:
            return
        self.c.table("purchases").update({"history": [*cur.history, entry], "updated_at": now()}).eq("purchase_id", purchase_id).execute()

    def list_stuck(self, older_than_s: float) -> list[Purchase]:
        res = (self.c.table("purchases").select("*").in_("payment_status", [str(s) for s in IN_FLIGHT])
               .lt("updated_at", now() - older_than_s).order("updated_at").execute())
        return [_row(d) for d in res.data]

    def spent_minor(self, principal_id: str) -> int:
        res = (self.c.table("purchases").select("authorized_amount_minor").eq("principal_id", principal_id)
               .in_("payment_status", [str(s) for s in BUDGET_STATES]).execute())
        return sum(int(d["authorized_amount_minor"] or 0) for d in res.data)

    # ---- evidence --------------------------------------------------------
    def add_evidence(self, purchase_id: str, kind: str, payload: dict[str, Any]) -> str:
        eid = new_id()
        self.c.table("evidence").insert({"evidence_id": eid, "purchase_id": purchase_id, "kind": str(kind), "payload": payload, "created_at": now()}).execute()
        return eid

    def list_evidence(self, purchase_id: str) -> list[Evidence]:
        res = self.c.table("evidence").select("*").eq("purchase_id", purchase_id).order("created_at").execute()
        return [Evidence(d["evidence_id"], d["purchase_id"], d["kind"], d["payload"], d["created_at"]) for d in res.data]

    # ---- findings --------------------------------------------------------
    def add_finding(self, purchase_id: str, code: str, severity: str, summary: str,
                    evidence_ids: list[str], safe_next_actions: list[str]) -> str:
        fid = new_id()
        self.c.table("findings").insert({"finding_id": fid, "purchase_id": purchase_id, "code": str(code), "severity": severity,
                                         "summary": summary, "evidence_ids": list(evidence_ids), "safe_next_actions": list(safe_next_actions),
                                         "created_at": now()}).execute()
        return fid

    def findings_for_purchase(self, purchase_id: str) -> list[Finding]:
        res = self.c.table("findings").select("*").eq("purchase_id", purchase_id).order("created_at").execute()
        return [_finding(d) for d in res.data]

    def has_finding(self, purchase_id: str, code: str) -> bool:
        res = self.c.table("findings").select("finding_id").eq("purchase_id", purchase_id).eq("code", str(code)).limit(1).execute()
        return bool(res.data)

    def _purchase_ids(self, principal_id: str, service_id: str | None = None) -> list[str]:
        q = self.c.table("purchases").select("purchase_id").eq("principal_id", principal_id)
        if service_id is not None:
            q = q.eq("service_id", service_id)
        return [d["purchase_id"] for d in q.execute().data]

    def has_unresolved_service_finding(self, principal_id: str, service_id: str, code: str) -> bool:
        ids = self._purchase_ids(principal_id, service_id)
        if not ids:
            return False
        res = self.c.table("findings").select("finding_id").in_("purchase_id", ids).eq("code", str(code)).is_("resolved_at", "null").limit(1).execute()
        return bool(res.data)

    def resolve_findings(self, purchase_id: str, code: str) -> int:
        res = self.c.table("findings").update({"resolved_at": now()}).eq("purchase_id", purchase_id).eq("code", str(code)).is_("resolved_at", "null").execute()
        return len(res.data)

    def resolve_service_findings(self, principal_id: str, service_id: str, code: str) -> int:
        ids = self._purchase_ids(principal_id, service_id)
        if not ids:
            return 0
        res = self.c.table("findings").update({"resolved_at": now()}).in_("purchase_id", ids).eq("code", str(code)).is_("resolved_at", "null").execute()
        return len(res.data)

    def list_findings(self, principal_id: str, unresolved_only: bool = True) -> list[dict[str, Any]]:
        purchases = {d["purchase_id"]: d for d in self.c.table("purchases").select(
            "purchase_id, service_id, payment_status, fulfillment_status, authorized_amount_minor, purchase_intent_id"
        ).eq("principal_id", principal_id).execute().data}
        if not purchases:
            return []
        q = self.c.table("findings").select("*").in_("purchase_id", list(purchases)).order("created_at")
        if unresolved_only:
            q = q.is_("resolved_at", "null")
        out = []
        for d in q.execute().data:
            f = _finding(d).to_dict()
            p = purchases[d["purchase_id"]]
            f.update(service_id=p["service_id"], payment_status=p["payment_status"], fulfillment_status=p["fulfillment_status"],
                     authorized_amount_minor=p["authorized_amount_minor"], purchase_intent_id=p["purchase_intent_id"])
            out.append(f)
        return out

    # ---- admin -----------------------------------------------------------
    def reset(self, principal_id: str) -> dict[str, int]:
        ids = self._purchase_ids(principal_id)
        counts = {"purchases": 0, "evidence": 0, "findings": 0}
        if not ids:
            return counts
        counts["evidence"] = len(self.c.table("evidence").delete().in_("purchase_id", ids).execute().data)
        counts["findings"] = len(self.c.table("findings").delete().in_("purchase_id", ids).execute().data)
        counts["purchases"] = len(self.c.table("purchases").delete().in_("purchase_id", ids).execute().data)
        return counts
```

- [ ] **Step 4: Run the gated test, flip, and re-run the step-5 checkpoint**

Run: `SUPABASE_URL=... SUPABASE_SERVICE_KEY=... uv run pytest tests/test_store_supabase.py -q`
Expected: 1 passed.

Set `BILLGUARD_STORE=supabase` in `.env`, restart Claude Desktop, run `uv run python scripts/reset_demo.py --yes`, then repeat Task 5.3 steps 1–2.
Expected: identical results; the Supabase table editor shows the `purchases`, `evidence` and `findings` rows appearing live.

- [ ] **Step 5: Commit**

```bash
git add billguard/store/supabase.py supabase/schema.sql tests/test_store_supabase.py
git commit -m "feat(store): Supabase repository behind the same protocol; env-gated test"
```

**Exit criteria (auto):**
- Run: `uv run pytest -q` → Expected: all pass, `test_store_supabase.py` reported as skipped when env is unset
- Run: `SUPABASE_URL=... SUPABASE_SERVICE_KEY=... uv run pytest tests/test_store_supabase.py -q` → Expected: `1 passed`
- Run: `uv run python -c "from billguard.store.supabase import SupabaseRepository; print('import ok')"` → Expected: `import ok`

**Manual check (M6 checkpoint):** scenario B passes with `BILLGUARD_STORE=supabase`; `list_findings` from a **fresh** Claude Desktop session (quit and relaunch) returns the `PAID_NO_DELIVERY` and `DUPLICATE_SUSPECTED` findings (metric 6). If the 45 min are gone, set `BILLGUARD_STORE=sqlite` back and continue.

### Milestone M6 — Checkpoint and manual verification

- [ ] Gated Supabase test passes against the project.
- [ ] Scenario B on Supabase matches the M5 checkpoint.
- [ ] Fresh Claude Desktop session → `list_findings` returns the findings (metric 6, also valid on SQLite).
- [ ] Decide and record in `.env` which store the demo runs on. Default stays `sqlite` if anything is off (§10).
- [ ] `uv run python scripts/reset_demo.py --yes`.

---

## Milestone M7 — Bazaar seller on Base mainnet `[W-MAINNET]`

**Timebox:** 60 min, hard. **Cut (§8.7):** if no purchase settles within the hour, use the written fallback (Task 7.2) and show the discovery listing plus the probe output; "the payment code path is identical."
**Checkpoint (§8.7):** one Bazaar purchase on Base mainnet settles through the same code path, or the written fallback is ready.

### Task 7.1: Register the Bazaar seller as the fourth paid tool `[W-MAINNET]`

**Maps to:** F1 (fourth paid tool), metric 7, design §4 public seller (winner registered with `non_empty` and `content_hash`), §6 `<public_seller_tool>`, §8.7, PRD goal 2.
**Timebox:** 40 min.
**SDK calls:** FastMCP `@mcp.tool(name=...)`; `purchase()` with the `bazaar` registry entry (same code path).
**§7 tests covered:** the schema test (still no force/retry param with the fourth tool present).

**Files:**
- Modify: `billguard/server.py` (add the env-gated tool block only), `.env` (operator), `.env.example` (document)
- Test: `tests/test_schema.py` is unchanged; add a gated check in `tests/test_bazaar_tool.py`

**Implementer brief:** The registry already adds a `bazaar` service when `BAZAAR_URL` is set (Task 2.3). Add the matching paid tool to `server.py`, registered only when the env is present, with the same `(purchase_intent_id, task_id)` shape and no force/retry parameter. The tool takes no service args: the probe picked a resource URL that works as a plain GET. The mainnet buyer key pays USDC only; the facilitator pays gas. The registry pins `eip155:8453`, so the Sepolia key can never be used here.

- [ ] **Step 1: Write the env values from the probe (operator)**

Run: `uv run python scripts/bazaar_probe.py --pick <winner_url_from_M1>` and paste the five printed lines into `.env`.

- [ ] **Step 2: Add the tool to `billguard/server.py`** (after `convert_document`)

```python
# ---- fourth paid tool: a public Bazaar seller on Base mainnet (design §4, §8.7). Registered iff BAZAAR_URL is set. ----
if os.environ.get("BAZAAR_URL"):
    _bazaar_tool_name = os.environ.get("BAZAAR_TOOL_NAME", "get_public_data")

    @mcp.tool(name=_bazaar_tool_name,
              description=f"Buy from a public x402 seller on Base mainnet discovered via the CDP Bazaar ({os.environ['BAZAAR_URL']}); "
                          f"price {int(os.environ['BAZAAR_PRICE_MINOR']) / 1_000_000:.4f} USDC. This seller has never heard of BillGuard. {REUSE}")
    async def get_public_data(purchase_intent_id: str, task_id: str | None = None) -> dict[str, Any]:
        return await purchase(get_deps(), "bazaar", {}, purchase_intent_id, task_id)
```
(`server.py` already loads `.env` at import time since Task 2.7, so this block is the only M7 edit to the file.)

- [ ] **Step 3: Write `tests/test_bazaar_tool.py`**

```python
import importlib
import os

import pytest


@pytest.mark.skipif(not os.environ.get("BAZAAR_URL"), reason="BAZAAR_URL not set")
async def test_bazaar_tool_registered_without_force_param():
    import billguard.server as srv

    importlib.reload(srv)
    tools = await srv.mcp.get_tools()
    name = os.environ.get("BAZAAR_TOOL_NAME", "get_public_data")
    assert name in tools
    props = set(tools[name].parameters["properties"])
    assert props == {"purchase_intent_id", "task_id"}
```

- [ ] **Step 4: Buy once on mainnet through Claude Desktop (operator)**

Restart Claude Desktop. Prompt: *"Use the public data tool to buy from the Bazaar seller, purchase intent id demo-bazaar-1."*
Expected: `SETTLED`, `fulfillment_status: VERIFIED` (validators `non_empty`, `content_hash`), `network: eip155:8453`, `explorer_url` on basescan.org, cost equal to `BAZAAR_PRICE_MINOR`. `get_spending_policy` shows the mainnet amount added to `spent_minor` (one budget across networks, as designed).

- [ ] **Step 5: Commit**

```bash
git add billguard/server.py tests/test_bazaar_tool.py .env.example
git commit -m "feat(server): env-gated fourth paid tool for the Bazaar seller on Base mainnet"
```

**Exit criteria (auto):**
- Run: `BAZAAR_URL=https://example.invalid/x BAZAAR_PRICE_MINOR=1000 uv run pytest tests/test_bazaar_tool.py tests/test_schema.py -q` → Expected: all pass
- Run: `uv run pytest -q` → Expected: all pass (bazaar test skipped when env unset)
- Run: `grep -c "'bazaard'\|\"bazaard\"" billguard/server.py` → Expected: `0` (sanity: service id is `bazaar`)

**Manual mainnet check (M7 checkpoint, metric 7):** step 4 above, plus the basescan link shows a USDC transfer from the mainnet buyer to the seller's `payTo`.

### Task 7.2: Written fallback for the Bazaar beat `[W-MAINNET]`

**Maps to:** design §8.7 "Written fallback", §10 "No usable v2 Bazaar seller, or mainnet funding delayed", PRD open question 1.
**Timebox:** 10 min (do it even if 7.1 succeeded; it costs nothing and covers a mainnet outage at demo time).
**SDK calls:** none. **§7 tests covered:** none (demo material).

**Files:**
- Create: `docs/billguard/demo/bazaar-fallback.md`

**Implementer brief (operator task):** Write the one-page fallback for demo step 8 so the Bazaar beat survives a mainnet outage: the probe listing, the one sentence that explains the code path is identical, and the hour-1 smoke receipt.

- [ ] **Step 1: Write the fallback**

```markdown
# Bazaar fallback (design §8.7)

Use when the mainnet purchase cannot be shown live.

1. Show `uv run python scripts/bazaar_probe.py` output (paste the latest run below).
2. Say: "These are strangers' x402 endpoints discovered through the CDP Bazaar. BillGuard registers one with two validators and buys through the SAME code path you just saw on Sepolia: quote → mandate → sign → send → verify. Nothing in the seller changes."
3. If a smoke-test receipt exists from hour 1, show it (paste below).

## Latest probe output
<paste>

## Hour-1 smoke receipt
<paste the SMOKE line(s) and basescan link>
```

- [ ] **Step 2: Commit**

```bash
git add docs/billguard/demo/bazaar-fallback.md
git commit -m "docs(demo): written fallback for the Bazaar beat"
```

**Exit criteria (auto):**
- Run: `test -f docs/billguard/demo/bazaar-fallback.md && echo ok` → Expected: `ok`
- Run: `grep -c '<paste>' docs/billguard/demo/bazaar-fallback.md` → Expected: `0` (both sections filled in)

### Milestone M7 — Checkpoint and manual verification

- [ ] Either a live mainnet purchase shows `SETTLED` + `VERIFIED` with a basescan link, or `docs/billguard/demo/bazaar-fallback.md` is filled in.
- [ ] `list_services` in Claude Desktop lists five services including `bazaar` on `eip155:8453`.
- [ ] The paid-tool schema still has no force/retry parameter (`uv run pytest tests/test_schema.py -q`).
- [ ] `uv run python scripts/reset_demo.py --yes`.

---

## Milestone M8 — Stretch `[W-STRETCH]`

Only after M7 (or its fallback). Each item is independent; do them in this order and stop at 06:00.

### Task 8.1: `scripts/approve_retry.py` — the human-only second payment `[W-STRETCH]`

**Maps to:** F2 and F12 (`approve_retry.py` is the only path), design §3 "Superseding a settled purchase (N2, S8)", §8.8, §9 step 9 "a second payment needs a human".
**Timebox:** 20 min.
**SDK calls:** `purchase.run_from_quote` (steps 2–7), which awaits `client.create_payment_payload`.
**§7 tests covered:** none (CLI; covered by the schema test's "no approve_retry tool" assertion).

**Files:**
- Create: `scripts/approve_retry.py`

**Implementer brief:** About 15 lines over `purchase.run_from_quote`. The operator passes a `purchase_id`. Refuse while the prior row is in flight or `SETTLEMENT_UNKNOWN` (print the `valid_before` deadline). Otherwise create `attempt = max + 1` with `supersedes_purchase_id` and `approved_by = "cli"`, resolve the service's `PAID_NO_DELIVERY` findings (lifting the quarantine), run steps 2–7, and print the response JSON. The new payment counts against the budget.

- [ ] **Step 1: Write the script**

```python
"""Human-only approval of a second payment for a superseded purchase (design §3 N2/S8). Not an MCP tool.

Usage: uv run python scripts/approve_retry.py <purchase_id> [--reason "..."]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from billguard.deps import get_deps
from billguard.models import IN_FLIGHT, FindingCode, PaymentStatus, Purchase
from billguard.purchase import CALL_BUDGET_S, run_from_quote
from billguard.response import build_response


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("purchase_id")
    ap.add_argument("--reason", default="operator approved re-purchase")
    args = ap.parse_args()
    deps = get_deps()
    repo = deps.repo
    prior = repo.get_purchase(args.purchase_id)
    if prior is None:
        print("purchase not found")
        return 1
    if prior.payment_status in IN_FLIGHT or prior.payment_status == PaymentStatus.SETTLEMENT_UNKNOWN:
        print(f"refused: {prior.purchase_id} is {prior.payment_status}; wait until valid_before={prior.valid_before} "
              f"(unix) and run reconcile_purchase first")
        return 2
    attempts = repo.find_by_key(prior.principal_id, prior.purchase_intent_id, prior.request_fingerprint)
    new = Purchase.new(principal_id=prior.principal_id, task_id=prior.task_id, purchase_intent_id=prior.purchase_intent_id,
                       request_fingerprint=prior.request_fingerprint, service_id=prior.service_id, args=prior.args,
                       network=prior.network, attempt=max(a.attempt for a in attempts) + 1,
                       supersedes_purchase_id=prior.purchase_id, approved_by="cli")
    new = repo.create_purchase(new)
    repo.append_history(new.purchase_id, {"state": "approved_by_cli", "reason": args.reason})
    lifted = repo.resolve_service_findings(prior.principal_id, prior.service_id, FindingCode.PAID_NO_DELIVERY)
    print(f"created attempt {new.attempt} ({new.purchase_id}) superseding {prior.purchase_id}; resolved {lifted} PAID_NO_DELIVERY finding(s)")
    row = await run_from_quote(deps, new, deps.registry[prior.service_id], prior.args, time.monotonic() + CALL_BUDGET_S)
    print(json.dumps(build_response(deps, row), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Prove it on Sepolia (operator)**

After a scenario-B run: `uv run python scripts/approve_retry.py <purchase_id_of_the_PAID_NO_DELIVERY_row>` → prints `created attempt 2 ...` and a response with `attempt: 2`, `supersedes_purchase_id` set, a new transfer on the explorer. Running it again while attempt 2 is `SETTLEMENT_UNKNOWN` prints `refused: ...`.

- [ ] **Step 3: Commit**

```bash
git add scripts/approve_retry.py
git commit -m "feat(scripts): approve_retry creates a human-approved superseding attempt"
```

**Exit criteria (auto):**
- Run: `grep -rn 'approve_retry' billguard/ | wc -l | tr -d ' '` → Expected: `0` (still no MCP path)
- Run: `uv run pytest tests/test_schema.py -q` → Expected: pass
- Run: `uv run python scripts/approve_retry.py does-not-exist; echo $?` → Expected: `purchase not found` then `1`

### Task 8.2: `--naive-retry` contrast rehearsal `[W-STRETCH]`

**Maps to:** design §8.8, §9 step 2 "optionally run --naive-retry".
**Timebox:** 10 min. **Files:** `docs/billguard/demo/checklist.md` (append the screenshot path). **SDK calls:** `x402HttpxClient` via the existing script. **§7 tests covered:** none (demo contrast).

**Implementer brief (operator task):** Produce the "bare SDK pays twice" contrast once, capture it, and decide whether it runs live or as a screenshot.

- [ ] Run `uv run python scripts/smoke_sellers.py --naive-retry` with the stall hook live. Expected: two `attempt N: PaymentError after paying: ...` lines about 60 s apart and **two** 0.75 USDC transfers on the explorer. Note the elapsed time; decide whether the 2-minute beat fits the 6-minute demo (default: show the explorer screenshot instead of running it live).
- [ ] Save the screenshot path in `docs/billguard/demo/checklist.md` (Task 9.1) under a line starting with `Naive-retry screenshot:`.

**Exit criteria (auto):**
- Run: `grep -c '^Naive-retry screenshot:' docs/billguard/demo/checklist.md` → Expected: `1`

### Task 8.3: Supabase table view as the live ledger `[W-STRETCH]`

**Maps to:** design §1 "the table view is the live-ledger stretch", §8.8. Only if M6 landed.
**Timebox:** 10 min. **Files:** `docs/billguard/demo/checklist.md` (append the dashboard URL). **SDK calls:** none. **§7 tests covered:** none.

**Implementer brief (operator task):** Prepare the Supabase table view that acts as the live ledger on the second screen during the demo.

- [ ] In the Supabase dashboard open `purchases` sorted by `created_at desc` with columns `service_id, payment_status, fulfillment_status, authorized_amount_minor, settlement_tx`; keep that tab on the second screen during the demo. Record the URL in `docs/billguard/demo/checklist.md` under a line starting with `Ledger view:`.

**Exit criteria (auto):**
- Run: `grep -c '^Ledger view: https://' docs/billguard/demo/checklist.md` → Expected: `1`

### Milestone M8 — Checkpoint and manual verification

**Checkpoint:** each stretch item that shipped is demonstrable; nothing that shipped broke the M5 checkpoint.
- [ ] `uv run pytest -q` green after any stretch commit.
- [ ] If 8.1 shipped: `approve_retry.py` on the scenario-B row created attempt 2 and a second transfer; the agent still has no tool for it (`test_schema.py` green).
- [ ] If 8.2 ran: the two-transfer screenshot is referenced in the checklist.
- [ ] If 8.3 ran: the ledger URL is in the checklist and the table updates during a purchase.
- [ ] `uv run python scripts/reset_demo.py --yes`.

---

## Milestone M9 — Pre-demo checklist (design §9) and fallback drills (design §10) `[W-DEMO]`

**Timebox:** 60 min (06:00–07:00). Nothing is cut here; this milestone is the demo's reliability.

### Task 9.1: Pre-demo checklist as a runnable document `[W-DEMO]`

**Maps to:** design §9 (pre-demo reset, steps 1–9), §8 "run reset between every rehearsal and as the last step before the demo", §7 "a cold-start Claude session with the spec prompt", metric 6 (fresh session), metric 8.
**Timebox:** 25 min including one full dry run.
**SDK calls:** none.
**§7 tests covered:** the manual list ("one Sepolia run per scenario, then a cold-start Claude session with the spec prompt").

**Implementer brief (operator task):** Turn design §9 into a checklist the presenter runs top to bottom 30 minutes before the demo, with the exact commands, the 6-minute script, and the prompt to paste. The reset is the last command. The dry run includes a cold-start Claude Desktop session with the product spec's §8 prompt pasted verbatim (the spec is the operator's `writing-block` file, outside this repo) to prove the guard does not depend on Claude reusing intent ids.

**Files:**
- Create: `docs/billguard/demo/checklist.md`

- [ ] **Step 1: Write the checklist**

```markdown
# Pre-demo checklist (run top to bottom, T-30 min)

## Environment
- [ ] `.env` present; `BILLGUARD_STORE` set to the store that passed the last rehearsal (sqlite unless M6 passed)
- [ ] `uv run python scripts/setup_wallets.py --check` → CHECK OK (buyer Sepolia USDC ≥ 4; mainnet USDC ≥ Bazaar price)
- [ ] Seller up: `uv run uvicorn sellers.app:app --port 8402`; `curl -s localhost:8402/health` ok; `SELLER_STALL_S=90`
- [ ] `uv run python scripts/smoke_sellers.py` → SMOKE OK (also proves the hook did not break /report and /convert)
- [ ] `uv run pytest -q` green
- [ ] Claude Desktop restarted; the `billguard` server lists 8 or 9 tools; none named approve_retry; no force/retry params
- [ ] Explorer tabs open: Sepolia seller pay_to address (token transfers), Base mainnet Bazaar payTo
- [ ] Backup video open and paused at 0:00 (`docs/billguard/demo/backup-video.md`)
- [ ] Bazaar fallback doc open (`docs/billguard/demo/bazaar-fallback.md`)
- [ ] Cold-start dry run done once: quit Claude Desktop, relaunch, paste the product spec §8 prompt verbatim (no fixed intent ids), confirm scenario B's "try again" is refused without a second transfer
- [ ] `uv run python scripts/reset_demo.py --yes` ← LAST COMMAND BEFORE THE DEMO (clears the duplicate guard and the spend)

## Script (6 minutes) — design §9
1. Hook: "Agents can already spend money. Who checks they got what they paid for?"
2. `smoke_sellers.py` on screen; `grep -rn billguard sellers/` is empty. Optional: `--naive-retry` screenshot (two transfers).
3. Claude Desktop, spec §8 prompt, budget $5.
4. Report → VERIFIED; click the Sepolia explorer link.
5. Research → SETTLEMENT_UNKNOWN → chain_check(true) → SETTLED, PAID_NO_DELIVERY, all in one call; show `history`.
   Presenter: "Try that research purchase again." → DUPLICATE_SUSPECTED (same intent) or SERVICE_QUARANTINED (reworded). One transfer.
6. Convert → 12 of 20 pages → FULFILLMENT_FAILED with page-count evidence. "Claude has no tool to pay again."
7. `list_findings` → Claude summarizes spent / delivered / unresolved.
8. Bazaar seller on mainnet: "this seller has never heard of us." (or the fallback doc)
9. Limits: no escrow, no refunds; schema-valid is not fact-checked; a second payment needs a human, the agent has no tool for it. (Name `approve_retry.py` only if Task 8.1 shipped.)

## Prompt for step 3 (paste into Claude Desktop)
"You have a $5 budget via BillGuard. Buy the ACME company report, then a research report on 'agent payments', then convert document contract-7 to a 20-page PDF. Use purchase intent ids demo-report-1, demo-research-1, demo-convert-1. After each purchase, tell me the payment status, what was delivered, and any findings. Finally list all findings and summarize what was spent, what was delivered, and what is unresolved."
```

- [ ] **Step 2: Run the checklist once end to end (full rehearsal), then commit**

```bash
git add docs/billguard/demo/checklist.md
git commit -m "docs(demo): pre-demo checklist and 6-minute script"
```

**Exit criteria (auto):**
- Run: `grep -c '^- \[x\]' docs/billguard/demo/checklist.md` → Expected: `11` or more after the dry run (the operator ticks boxes in the file as they pass)
- Run: `grep -c 'reset_demo.py --yes' docs/billguard/demo/checklist.md` → Expected: `1` or more
- Run: `grep -c '^## Prompt for step 3' docs/billguard/demo/checklist.md` → Expected: `1`
- Manual: the rehearsal finishes in under 7 minutes; the last command run was `reset_demo.py --yes`.

### Task 9.2: Fallback drills (design §10), one per risk `[W-DEMO]`

**Maps to:** design §10 (all five risks), §8.1 and §8.7 fallbacks, non-functional facilitator fallback.
**Timebox:** 25 min total. **Files:** append a "Fallbacks" section to `docs/billguard/demo/checklist.md`.
**SDK calls:** none. **§7 tests covered:** none (operational drills).

**Implementer brief (operator task):** Rehearse each design §10 fallback once so that on stage the switch is a reflex, and write the one-line instruction for each into the checklist's `## Fallbacks` section.

- [ ] **Drill A — facilitator or RPC flakiness:** set `SELLER_FACILITATOR=local` in `.env`, restart the seller, run `smoke_sellers.py` → `SMOKE OK`. Switch back to `remote`. Confirm the backup video plays. Write down: "if x402.org fails on stage: flip to local (30 s), else play the video."
- [ ] **Drill B — Supabase or Wi-Fi trouble:** set `BILLGUARD_STORE=sqlite`, restart Claude Desktop, run one report purchase → `VERIFIED`. Write down: "sqlite is the default; nothing else changes."
- [ ] **Drill C — no usable Bazaar seller or mainnet funding delayed:** open `docs/billguard/demo/bazaar-fallback.md`, confirm the probe output is pasted. Write down the one sentence to say.
- [ ] **Drill D — stale rehearsal state:** run scenario B without a reset → observe `DUPLICATE_SUSPECTED` on the first research call (the stall does not show). Run `reset_demo.py --yes` → scenario B works. Write down: "reset is the last command before the demo."
- [ ] **Drill E — hook regression:** after any seller edit, `smoke_sellers.py` must show `/report` and `/convert` delivering. Write down: "never edit `sellers/` on demo day."
- [ ] Append the five written lines under `## Fallbacks` in `docs/billguard/demo/checklist.md` and commit:

```bash
git add docs/billguard/demo/checklist.md
git commit -m "docs(demo): fallback drills for design §10 risks"
```

**Exit criteria (auto):**
- Run: `grep -c '^## Fallbacks' docs/billguard/demo/checklist.md` → Expected: `1`
- Run: `sed -n '/^## Fallbacks/,$p' docs/billguard/demo/checklist.md | grep -c '^- '` → Expected: `5`

### Milestone M9 — Final verification (the demo acceptance metrics, PRD §7)

- [ ] **Metric 1:** `smoke_sellers.py` pays `/report` and `/convert` with the plain SDK client; `grep -rn billguard sellers/` is empty.
- [ ] **Metric 2:** from Claude Desktop, one real Sepolia settlement returns `SETTLED` + `VERIFIED` with a transaction hash.
- [ ] **Metric 3:** `/report-premium` (tier premium) yields `PRICE_MISMATCH` with no signed payload (`get_purchase` shows no `payment_payload` evidence).
- [ ] **Metric 4:** `/research-slow` yields `SETTLEMENT_UNKNOWN` then `SETTLED` + `PAID_NO_DELIVERY` within one tool call; "try again" yields `DUPLICATE_SUSPECTED` (same or new intent) or `SERVICE_QUARANTINED` (reworded); exactly one on-chain transfer.
- [ ] **Metric 5:** `/convert` yields `FULFILLMENT_FAILED` citing page count 12 vs 20.
- [ ] **Metric 6:** `list_findings` from a fresh Claude session returns the above findings.
- [ ] **Metric 7:** one Bazaar purchase on Base mainnet settles through the same code path, or the written fallback is shown.
- [ ] **Metric 8:** paid-tool schemas contain no force-retry parameter and no tool named `approve_retry` exists (`uv run pytest tests/test_schema.py -q`).

---

## Appendix A — Requirement coverage map

| Requirement | Tasks |
|---|---|
| F1 four paid tools + five admin tools | 2.7 (3 paid + 5 admin), 7.1 (Bazaar tool) |
| F2 no force-retry; only `approve_retry.py` | 2.7, 3.3 (schema test), 8.1 |
| F3 state machine, CAS before each network call, header stored before send | 2.1, 2.2, 2.6 |
| F4 quote rejections before signing | 2.3 (mandate), 2.6 (NOT_X402, version, asset, price, budget) |
| F5 only 2xx+success=true is SETTLED; rest reconciled same call | 2.6 (`classify_outcome`, `reconcile_and_finish`), 5.2 |
| F6 reconciliation rules, 45 s budget | 5.2 (poll, resend once, block timestamp), 2.6 (`CALL_BUDGET_S`) |
| F7 duplicate guard both variants, 15 min, quarantine | 3.1, 5.4 (end to end) |
| F8 validators, FULFILLMENT_FAILED/UNVERIFIED, PAID_NO_DELIVERY | 2.4, 4.1, 2.6 (`_verify`) |
| F9 budget sum over states, integer minor units | 2.1, 2.2 (`spent_minor`), 2.3 |
| F10 startup recovery | 2.7 (lifespan task), 3.2 |
| F11 demo sellers | 1A.2, 5.1 (stall hook) |
| F12 scripts | 1A.1, 1A.3, 1B.1, 2.8, 8.1 |
| F13 Repository + SQLite + Supabase | 2.2, 6.1 |
| NF per-service timeouts 15/40 s, stall 90 s, validBefore ~300 s | 2.3, 1A.2 (`max_timeout_seconds=300`), 5.1 |
| NF stderr logging, WAL + busy timeout | 0.1, 2.2, 2.7 |
| NF keys from env only, signed auths stored deliberately | 1A.1, 2.3 (wallet), 2.6 (payment_payload evidence) |
| NF facilitator default + local fallback that settles on-chain | 1A.2 |
| NF every finding references evidence; severity proven/suspected | 2.6, 3.1, 4.1, 5.2 |
| Metrics 1–8 | M9 final verification (above); metric 4 also by test 5.4 |
| Design §8 build order and checkpoints | M1–M8 headers |
| Design §9 demo script | 9.1 |
| Design §10 risks | 9.2 |
| Review S1–S9 | S1 → 3.1; S2 → 2.3; S3 → 3.1; S4 → 2.6/5.2; S5 → 5.2; S6 → 1B.1; S7 → 0.1/2.2/2.7; S8 → 2.7/3.3/8.1; S9 → 2.6 (await), 5.1 (adapter+method in hook test) |
| PRD open questions | Q1 → 1B.1/7.1; Q2 → 1A.2/9.2 drill A; Q3 → 6.1 (flip after step 5; `BILLGUARD_STORE` any time); Q4 → 2.4/4.1 (paths for all PDFs); Q5 closed (3.1 uses 15 min; 2.8 resets) |

## Appendix B — Plan decisions (inside the design's constraints)

1. `get_company_report(..., tier="premium")` routes to `/report-premium`. Keeps F1's four paid tools; adds no force/retry parameter.
2. Rows carry `args` and `reject_reason` columns (needed for the identical resend, `approve_retry.py`, and non-finding reject codes such as `SIGNING_FAILED`).
3. `INTENT_RECORDED -> REJECTED` is a legal transition for rejections that happen before quoting (`NOT_X402`, unsupported version, `SERVICE_QUARANTINED`, `POLICY_DENIED`).
4. `outcome.py` holds `send_paid_request`/`classify_outcome` so `reconcile.py` can reuse step 6 without a circular import.
5. The stall hook lives in `sellers/stall.py` so it can be unit-tested without contacting the facilitator; `sellers/app.py` registers it with one line.
6. The quarantine (S3) also applies to `attempt+1` rows, which is stricter than the letter of §3 and inside its intent.
7. The Bazaar discovery client needs the `x402[extensions]` extra (jsonschema), pinned in `pyproject.toml`.
8. **Build order vs frozen interfaces.** M2 lays down the lock, compare-and-set writes, the mandate check, finding creation and the `history` array as part of the core flow (design §8 lists some of these under steps 3–5). The §8 checkpoints are unchanged; M3–M5 fill bodies and add tests. This is what makes steps 3–5 parallel, as §8 requires.
9. **M1 hard stop overlaps M2.** The 23:00 hard stop for the first Sepolia settlement falls inside M2's window; M2 starts at 22:20 regardless because its suite runs against a fake seller. Track A keeps retrying the facilitator in parallel until 23:00, then flips to `local`.
10. **In-flight same-intent repeats get `DUPLICATE_SUSPECTED`.** Design §3 step 1 says "return it with IN_PROGRESS"; PRD F7 and design §0 (S1) say in-flight repeats attach the finding. The plan does both: the finding is attached and the response still shows `IN_PROGRESS`.
11. **Duplicate guard runs before the quarantine.** So a same-topic retry under a new intent id returns the existing record with `DUPLICATE_SUSPECTED` (PRD metric 4, user story), and only a reworded request hits `SERVICE_QUARANTINED`. The quarantine row carries its own `retry_blocked` evidence so every finding references evidence on its own purchase.
12. **`approve_retry.py` is M8 stretch** as design §8 orders it, even though PRD F12 lists it as a script and the PRD user story names it. If it does not ship, demo step 9 uses the design's wording ("a second payment needs a human; the agent has no tool for it"). It is about 15 lines over `run_from_quote`; do it first in M8.
13. **`QUOTE_FAILED` reject reason.** A transport error on the unpaid request is not proof that a route is unprotected, so it is rejected without a proven `NOT_X402` finding. Nothing is signed either way.
14. **45 s budget.** Quote and send timeouts are clipped to the remaining call budget, and each chain read is wrapped in `asyncio.wait_for` against it. The reconcile resend uses an 8 s cap (S4). No pytest covers wall-clock time; Task 5.3's live timing (scenario B under 45 s) is the check.
