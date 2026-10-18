"""Buyer signer per network from env (design §2). Private keys never leave this module."""
from __future__ import annotations

import os
from functools import lru_cache

from eth_account import Account
from eth_account.signers.local import LocalAccount
from x402.client import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

from payguard.registry import MAINNET, SEPOLIA

_KEY_ENV = {SEPOLIA: "BUYER_KEY_SEPOLIA", MAINNET: "BUYER_KEY_MAINNET"}


def buyer_account(network: str) -> LocalAccount:
    env = _KEY_ENV.get(network)
    if env is None:
        raise ValueError(f"no buyer key mapping for network {network}")
    key = os.environ.get(env)
    if not key:
        raise RuntimeError(f"{env} is not set")
    return Account.from_key(key)


def client_for_account(account: LocalAccount, network: str) -> x402Client:
    client = x402Client()
    client.register(network, ExactEvmScheme(EthAccountSigner(account)))
    client.set_spend_controls({"max_amount_per_payment": False})  # PayGuard owns the mandate (design §2)
    return client


@lru_cache(maxsize=4)
def build_client(network: str) -> x402Client:
    return client_for_account(buyer_account(network), network)


def buyer_address(network: str) -> str:
    return buyer_account(network).address
