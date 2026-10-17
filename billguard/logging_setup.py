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
