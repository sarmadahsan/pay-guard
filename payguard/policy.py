"""Spending mandate (design §2 policy fields, §3 step 3). Enforced before anything is signed."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from payguard.models import RejectReason
from payguard.registry import Service


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
    if type(amount_minor) is not int or amount_minor <= 0:
        raise MandateError(RejectReason.PRICE_MISMATCH,
                           f"quoted amount {amount_minor!r} is not a positive integer of minor units")
    if type(spent_minor) is not int or spent_minor < 0:
        raise ValueError(f"spent_minor must be a non-negative int, got {spent_minor!r}")
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
