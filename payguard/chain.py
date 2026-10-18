"""EIP-3009 settlement proof on the USDC contract (design §5, Appendix B.20). Chain time, not local time (S5).

authorizationState(from, nonce) == true means the nonce was consumed by transferWithAuthorization OR
cancelAuthorization. Money is proven to have moved only when an AuthorizationUsed(from, nonce) event is
found and the same transaction holds a USDC Transfer(from -> payTo, value). AuthorizationCanceled(from, nonce)
proves nothing can ever move. Public RPCs reject large eth_getLogs ranges (Base Sepolia allows 200 blocks), so
logs are searched in 100-block windows starting from an estimate of the block the purchase started in.
"""
from __future__ import annotations

import asyncio
import contextvars
import math
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from web3 import Web3
from x402.mechanisms.evm.constants import AUTHORIZATION_STATE_ABI

from payguard.registry import MAINNET, SEPOLIA, USDC

RPC_ENV = {SEPOLIA: ("RPC_URL_SEPOLIA", "https://sepolia.base.org"), MAINNET: ("RPC_URL_MAINNET", "https://mainnet.base.org")}
USER_AGENT = "payguard/0.1"  # the Base public RPC answers 403 to requests without a User-Agent
BLOCK_TIME_S = 2.0  # Base
LOG_WINDOW = 100  # blocks per eth_getLogs call (public RPC limit is 200)
MAX_WINDOWS = 60  # 6000 blocks ≈ 3.3 h: more than any validBefore window
START_MARGIN_BLOCKS = 60
READ_BUDGET_S = 30.0  # default bound on one authorization_state call when no call deadline is set

# Monotonic deadline of the current tool call. reconcile sets it around each read so the worker thread stops
# its log search when the call budget is spent, even though asyncio cannot cancel the thread itself.
CALL_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("payguard_chain_call_deadline", default=None)

TOPIC_USED = "0x" + Web3.keccak(text="AuthorizationUsed(address,bytes32)").hex()
TOPIC_CANCELED = "0x" + Web3.keccak(text="AuthorizationCanceled(address,bytes32)").hex()
TOPIC_TRANSFER = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()


@dataclass(frozen=True)
class ChainState:
    used: bool
    block_number: int
    block_timestamp: int
    event: str | None = None  # "used" | "canceled" | None (nonce consumed but no event found in the searched range)
    tx_hash: str | None = None  # transaction that emitted the event
    transfer_ok: bool | None = None  # that tx holds USDC Transfer(from -> payTo, value)
    # False when the log search stopped (call deadline or MAX_WINDOWS) before covering the whole range: then
    # event=None means "not looked at", not "no event exists". Only meaningful when used is True.
    search_complete: bool = True
    # Block of the matching AuthorizationUsed/Canceled log (the tx's block). block_number above is the head block
    # the check read from, which is what expiry logic needs; this is what a settlement is reported at.
    event_block: int | None = None


@lru_cache(maxsize=4)
def _w3(network: str) -> Web3:
    env, default = RPC_ENV[network]
    return Web3(Web3.HTTPProvider(os.environ.get(env) or default,
                                  request_kwargs={"timeout": 10, "headers": {"User-Agent": USER_AGENT}}))


def _hex(b: Any) -> str:
    """0x-prefixed lowercase hex for HexBytes / bytes / str."""
    if isinstance(b, str):
        return b.lower() if b.startswith("0x") else "0x" + b.lower()
    return "0x" + bytes(b).hex()


def _topic_addr(addr: str) -> str:
    return "0x" + "00" * 12 + addr[2:].lower()


def _is_transfer(lg: Any, usdc: str, from_addr: str, pay_to: str, value_minor: int) -> bool:
    topics = [_hex(t) for t in lg["topics"]]
    return (str(lg["address"]).lower() == usdc.lower() and len(topics) == 3 and topics[0] == TOPIC_TRANSFER
            and topics[1] == _topic_addr(from_addr) and topics[2] == _topic_addr(pay_to)
            and int.from_bytes(bytes(lg["data"]), "big") == int(value_minor))


def _find_event(w3: Any, usdc: str, from_addr: str, nonce_hex: str, pay_to: str, value_minor: int,
                from_block: int, to_block: int, stop_at: float | None = None
                ) -> tuple[str | None, str | None, bool | None, bool, int | None]:
    """Search AuthorizationUsed / AuthorizationCanceled for (from, nonce) in small windows; verify the Transfer.
    Returns (event, tx_hash, transfer_ok, search_complete, event_block). stop_at is a time.monotonic() value: once it passes
    the search stops and reports search_complete=False, which is never the same as "nothing found"."""
    start = max(0, from_block)
    for _ in range(MAX_WINDOWS):
        if start > to_block:
            break
        if stop_at is not None and time.monotonic() >= stop_at:
            return None, None, None, False, None
        end = min(start + LOG_WINDOW - 1, to_block)
        logs = w3.eth.get_logs({"address": usdc, "fromBlock": start, "toBlock": end,
                                "topics": [[TOPIC_USED, TOPIC_CANCELED], _topic_addr(from_addr), nonce_hex]})
        if logs:
            lg = logs[0]
            tx_hash = _hex(lg["transactionHash"])
            event_block = int(lg["blockNumber"])
            if _hex(lg["topics"][0]) == TOPIC_CANCELED:
                return "canceled", tx_hash, False, True, event_block
            receipt = w3.eth.get_transaction_receipt(lg["transactionHash"])
            ok = any(_is_transfer(t, usdc, from_addr, pay_to, value_minor) for t in receipt["logs"])
            return "used", tx_hash, ok, True, event_block
        start = end + 1
    return None, None, None, start > to_block, None  # MAX_WINDOWS exhausted before to_block: incomplete


def _read(network: str, from_addr: str, nonce: str, pay_to: str, value_minor: int, since_ts: float,
          stop_at: float | None = None) -> ChainState:
    w3 = _w3(network)
    usdc = Web3.to_checksum_address(USDC[network])
    block = w3.eth.get_block("latest")
    bn, ts = int(block["number"]), int(block["timestamp"])  # S5: one block for both the state read and the clock
    nonce_hex = _hex(nonce)
    contract = w3.eth.contract(address=usdc, abi=AUTHORIZATION_STATE_ABI)
    used = bool(contract.functions.authorizationState(Web3.to_checksum_address(from_addr),
                                                      bytes.fromhex(nonce_hex[2:])).call(block_identifier=bn))
    if not used:
        return ChainState(False, bn, ts)
    from_block = bn - math.ceil(max(0.0, time.time() - since_ts) / BLOCK_TIME_S) - START_MARGIN_BLOCKS
    event, tx_hash, ok, complete, event_block = _find_event(w3, usdc, from_addr, nonce_hex, pay_to, int(value_minor),
                                               from_block, bn, stop_at)
    return ChainState(True, bn, ts, event, tx_hash, ok, complete, event_block)


async def authorization_state(network: str, from_addr: str, nonce: str, pay_to: str, value_minor: int,
                              since_ts: float, *, stop_at: float | None = None) -> ChainState:
    """Non-blocking wrapper: web3's HTTP provider is synchronous.

    The worker thread cannot be cancelled, so it gets an absolute monotonic stop time: the explicit stop_at, else
    the call deadline in CALL_DEADLINE, else now + READ_BUDGET_S (whichever is earliest)."""
    limit = time.monotonic() + READ_BUDGET_S
    for t in (stop_at, CALL_DEADLINE.get()):
        if t is not None:
            limit = min(limit, t)
    return await asyncio.to_thread(_read, network, from_addr, nonce, pay_to, value_minor, since_ts, limit)
