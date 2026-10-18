"""mcpb/manifest.json (the Claude Desktop extension) launches the unchanged stdio server and carries no secret values."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.build_mcpb import OUT, SECRET_ENV
from scripts.export_tool_schema import OUT as TOOLS_JSON, find_key_like

REPO = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    return json.loads(OUT.read_text())


def test_manifest_advertises_exactly_the_exported_tools():
    exported = [{"name": t["name"], "description": t["description"]} for t in json.loads(TOOLS_JSON.read_text())["tools"]]
    m = _manifest()
    assert m["tools"] == exported
    assert m["tools_generated"] is False
    assert not any("approve" in t["name"] or "force" in t["name"] for t in m["tools"])


def test_manifest_launches_the_stdio_server_from_the_users_checkout():
    cfg = _manifest()["server"]["mcp_config"]
    assert cfg["command"] == "${user_config.uv_path}"
    assert cfg["args"] == ["run", "--directory", "${user_config.repo_dir}", "python", "-m", "payguard.server"]


def test_only_the_buyer_key_is_passed_and_it_is_a_sensitive_user_config_field():
    m = _manifest()
    assert m["server"]["mcp_config"]["env"] == {SECRET_ENV: "${user_config.buyer_key_sepolia}"}
    field = m["user_config"]["buyer_key_sepolia"]
    assert field["sensitive"] is True and field["required"] is True and "default" not in field
    for key, opt in m["user_config"].items():
        if key != "buyer_key_sepolia":
            assert not opt.get("sensitive"), key
    assert find_key_like(m) == []


def test_committed_manifest_is_up_to_date():
    r = subprocess.run([sys.executable, "scripts/build_mcpb.py", "--check"], cwd=REPO, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
