"""In-process SDK facilitator (design §4, Sugg. 3). Used iff SELLER_FACILITATOR=local.

Settles on-chain with FACILITATOR_KEY_SEPOLIA, which must hold a little Base Sepolia ETH.
The buyer still needs no ETH. Imports nothing from the guard package.
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
