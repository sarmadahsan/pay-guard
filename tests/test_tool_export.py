"""mcp-tools.json (the listing's advertised tool surface) keeps the guarantees tests/test_schema.py enforces on the server."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.export_tool_schema import OUT, find_key_like
from tests.test_schema import ADMIN_TOOLS, PAID_TOOLS

REPO = Path(__file__).resolve().parents[1]
GATED_PAID = ("get_intel_report", "get_public_data")  # env-gated; the export sets placeholders so both appear


def _doc() -> dict:
    return json.loads(OUT.read_text())


def test_export_has_no_approve_retry_and_no_force_or_retry_parameter():
    tools = {t["name"]: t for t in _doc()["tools"]}
    names = set(tools)
    assert names == set(PAID_TOOLS) | set(ADMIN_TOOLS) | set(GATED_PAID), names
    assert "approve_retry" not in names and not any("approve" in n or "force" in n for n in names)
    for n, t in tools.items():
        props = set(t["parameters"].get("properties", {}))
        assert not any("force" in p.lower() or "retry" in p.lower() for p in props), (n, props)
        if n in PAID_TOOLS or n in GATED_PAID:
            assert "purchase_intent_id" in props, (n, props)


def test_export_launches_stdio_server_and_names_env_without_values():
    doc = _doc()
    assert doc["transport"] == "stdio"
    assert doc["command"] == ["uv", "run", "--directory", "<repo>", "python", "-m", "payguard.server"]
    env = {**doc["env"]["required"], **doc["env"]["optional"]}
    assert "BUYER_KEY_SEPOLIA" in doc["env"]["required"]
    assert all(k.isupper() for k in env)  # names; the values are human descriptions, never config
    assert find_key_like(doc) == []


def test_key_scan_catches_a_key_shaped_string():
    fake = "0x" + "ab" * 32
    assert find_key_like({"tools": [{"description": f"key {fake}"}]})
    assert find_key_like({"x": "ab" * 32})
    assert not find_key_like({"addr": "0x000000000000000000000000000000000000dEaD"})


def test_committed_export_is_up_to_date():
    # A subprocess: tool registration is decided at import time, and this process already imported the server without
    # the gated tools.
    r = subprocess.run([sys.executable, "scripts/export_tool_schema.py", "--check"], cwd=REPO, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
