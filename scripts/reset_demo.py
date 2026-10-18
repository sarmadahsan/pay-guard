"""Wipe PayGuard demo state between rehearsals (design §8). Refuses to run without --yes.

Deletes the configured principal's purchases, evidence and findings on the active store
(PAYGUARD_STORE). Clears the 15-minute duplicate guard, the per-principal spend and the
wallet-audit baseline (the next purchase or list_purchases takes a fresh one).
On-chain transfers are not undone. Artifact files are left on disk.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/reset_demo.py` puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="required; actually delete")
    args = ap.parse_args()
    if not args.yes:
        print("refusing to reset without --yes")
        return 2
    from payguard.deps import get_deps
    from scripts.setup_wallets import main as wallets_main

    deps = get_deps()
    counts = deps.repo.reset(deps.policy.principal_id)
    print(f"reset principal {deps.policy.principal_id}: {counts}")
    print(f"spent now: {deps.repo.spent_minor(deps.policy.principal_id)} minor; budget {deps.policy.total_budget_minor} minor")
    print("balances:")
    sys.argv = ["setup_wallets.py", "--check"]
    return 0 if wallets_main() in (0, 1) else 1


if __name__ == "__main__":
    sys.exit(main())
