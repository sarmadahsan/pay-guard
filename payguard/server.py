"""PayGuard MCP server over stdio (design §6). stdout is JSON-RPC; all logs go to stderr."""
from __future__ import annotations

from payguard.logging_setup import configure_logging

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

from payguard import recovery  # noqa: E402
from payguard.deps import get_deps  # noqa: E402
from payguard.models import PaymentStatus  # noqa: E402
from payguard.purchase import CALL_BUDGET_S, purchase, reconcile_and_finish  # noqa: E402
from payguard.response import build_response  # noqa: E402

log = logging.getLogger("payguard.server")

INSTRUCTIONS = (
    "PayGuard buys from x402 sellers on your behalf and verifies what was delivered. "
    "Always pass a purchase_intent_id you made up for the task, and REUSE the same purchase_intent_id when you retry "
    "the same purchase. Read `findings` and `safe_next_actions` in every result before spending again. "
    "PayGuard never pays twice for the same purchase and has no tool that can make a second payment; a human must approve that."
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


mcp = FastMCP("payguard", instructions=INSTRUCTIONS, lifespan=lifespan)


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
