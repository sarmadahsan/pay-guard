"""Allowlisted services (design §2). Hand-written; four demo services plus two env-gated ones (Meridian Intel, Bazaar)."""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS

logger = logging.getLogger(__name__)

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
                "application/json", ("not_truncated", "content_type", "non_empty", "json_schema:company_report", "ticker_matches"), 40.0),
        Service("report_premium", "get_company_report", "GET", f"{base}/report-premium/{{ticker}}", SEPOLIA, usdc, 500_000,
                "application/json", ("not_truncated", "content_type", "non_empty", "json_schema:company_report", "ticker_matches"), 40.0),
        Service("research", "get_research_report", "GET", f"{base}/research-slow?topic={{topic}}", SEPOLIA, usdc, 750_000,
                "application/json", ("not_truncated", "content_type", "non_empty", "json_schema:research_report"), 15.0),  # S2
        Service("convert", "convert_document", "POST", f"{base}/convert", SEPOLIA, usdc, 2_000_000,
                "application/pdf", ("not_truncated", "content_type", "non_empty", "pdf_page_count"), 40.0),
    ]
    # --- Meridian Intel (hackathon demo): an adversarial Sepolia seller that settles, then answers 402 "Payment failed...
    # retry" (double-charge trap). Registered only when INTEL_BASE_URL is set; it runs on its own port (8403 by default)
    # because both seller apps default to 8402. Its /dossier route is deliberately NOT registered: the paid route only
    # ever returns a "processing" receipt, so there is no real dossier shape to write a json_schema validator against.
    intel_url = os.environ.get("INTEL_BASE_URL")
    if intel_url:
        intel = intel_url.rstrip("/")
        services.append(Service("intel", "get_intel_report", "GET", f"{intel}/intel/{{ticker}}", SEPOLIA, usdc, 500_000,
                                "application/json", ("not_truncated", "content_type", "non_empty", "ticker_matches"), 40.0))
    # --- Bazaar seller (M7): registered only when the probe's env lines are present ---
    bazaar_url = os.environ.get("BAZAAR_URL")
    if bazaar_url:
        raw_price = os.environ.get("BAZAAR_PRICE_MINOR")
        try:
            price = int(raw_price) if raw_price is not None else 0
        except ValueError:
            price = 0
        if price <= 0:
            logger.warning("bazaar service skipped: BAZAAR_PRICE_MINOR must be a positive integer, got %r", raw_price)
        else:
            services.append(Service(
                "bazaar", os.environ.get("BAZAAR_TOOL_NAME", "get_public_data"), "GET", bazaar_url, MAINNET, USDC[MAINNET],
                price, os.environ.get("BAZAAR_CONTENT_TYPE", "application/json"),
                ("not_truncated", "non_empty", "content_hash"), 40.0,
            ))
    return {s.id: s for s in services}
