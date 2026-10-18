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
