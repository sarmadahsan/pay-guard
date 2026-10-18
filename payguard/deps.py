"""Build the runtime Deps once: env, store, policy, registry, wallet, HTTP client factory."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from payguard import wallet
from payguard.policy import load_policy
from payguard.purchase import Deps
from payguard.registry import load_registry
from payguard.store import open_repository

ROOT = Path(__file__).resolve().parents[1]
_DEPS: Deps | None = None


def _abs(env: str, default: str) -> Path:
    p = Path(os.environ.get(env, default))
    return p if p.is_absolute() else ROOT / p


def build_deps() -> Deps:
    load_dotenv(ROOT / ".env")
    return Deps(
        repo=open_repository(ROOT),
        policy=load_policy(_abs("PAYGUARD_POLICY", "policy.yaml")),
        registry=load_registry(),
        client_factory=wallet.build_client,
        http_factory=lambda svc: httpx.AsyncClient(timeout=svc.timeout_s),
        artifact_dir=_abs("PAYGUARD_ARTIFACT_DIR", "artifacts"),
    )


def get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = build_deps()
    return _DEPS
