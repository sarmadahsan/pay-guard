"""Write mcpb/manifest.json, the Claude Desktop extension (.mcpb) manifest, from the committed mcp-tools.json.

The bundle carries no server code and no secrets. Claude Desktop launches the unchanged stdio server from the operator's
own checkout (`uv run --directory <repo> python -m payguard.server`), so the repo's .env, policy.yaml and ledger keep
working. The only value Claude Desktop passes in is BUYER_KEY_SEPOLIA, from a sensitive user_config field (masked and
stored securely by the client); every other variable is named in the description and read from <repo>/.env as before
(load_dotenv does not override variables already set, so the Claude Desktop value wins over a key in .env).

    uv run python scripts/build_mcpb.py            # writes mcpb/manifest.json
    uv run python scripts/build_mcpb.py --check    # exit 1 if the committed manifest is stale
    npx -y @anthropic-ai/mcpb@2.1.2 validate mcpb/manifest.json
    npx -y @anthropic-ai/mcpb@2.1.2 pack mcpb dist/payguard.mcpb

Spec notes and the date they were read: docs/pay-guard/demo/mcpb.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # run as a file: make `scripts.` importable like it is under pytest
    sys.path.insert(0, str(REPO))

from scripts.export_tool_schema import OUT as TOOLS_JSON, find_key_like  # noqa: E402

OUT = REPO / "mcpb" / "manifest.json"
MANIFEST_VERSION = "0.3"
REPO_URL = "https://github.com/sarmadahsan/pay-guard"

# The only env value the bundle sets. Everything else stays in <repo>/.env and is listed by name only.
SECRET_ENV = "BUYER_KEY_SEPOLIA"


def _long_description(tools_doc: dict[str, Any]) -> str:
    env = tools_doc["env"]
    required = [k for k in env["required"] if k != SECRET_ENV]
    gated = ", ".join(f"`{t}` needs {v}" for t, v in tools_doc["env_gated_tools"].items())
    return "\n".join([
        "PayGuard runs locally over stdio from your own pay-guard checkout. Wallet keys never leave your machine.",
        "",
        f"Claude Desktop passes only `{SECRET_ENV}`, stored as a sensitive setting. Put every other variable in "
        "`<checkout>/.env` (see `.env.example`):",
        "",
        "- Required: " + ", ".join(f"`{k}`" for k in required),
        "- Optional: " + ", ".join(f"`{k}`" for k in env["optional"]),
        "",
        f"Env-gated tools appear only when configured: {gated}.",
    ])


def build() -> dict[str, Any]:
    tools_doc = json.loads(TOOLS_JSON.read_text())
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    return {
        "manifest_version": MANIFEST_VERSION,
        "name": "payguard",
        "display_name": "PayGuard",
        "version": version,
        "description": tools_doc["description"],
        "long_description": _long_description(tools_doc),
        "author": {"name": "sarmadahsan", "url": "https://github.com/sarmadahsan"},
        "repository": {"type": "git", "url": f"{REPO_URL}.git"},
        "homepage": REPO_URL,
        "support": f"{REPO_URL}/issues",
        "server": {
            # Nominal: the bundle ships no Python. mcp_config runs the server module from the user's checkout.
            "type": "python",
            "entry_point": "payguard/server.py",
            "mcp_config": {
                "command": "${user_config.uv_path}",
                "args": ["run", "--directory", "${user_config.repo_dir}", "python", "-m", "payguard.server"],
                "env": {SECRET_ENV: "${user_config.buyer_key_sepolia}"},
            },
        },
        "tools": [{"name": t["name"], "description": t["description"]} for t in tools_doc["tools"]],
        "tools_generated": False,
        "keywords": ["x402", "payments", "buyer-protection", "base", "usdc"],
        "compatibility": {"platforms": ["darwin", "linux"], "runtimes": {"python": ">=3.12"}},
        "user_config": {
            "repo_dir": {
                "type": "directory",
                "title": "pay-guard checkout",
                "description": "Your local clone of pay-guard (after `uv sync` and filling .env). The server runs from here.",
                "required": True,
            },
            "buyer_key_sepolia": {
                "type": "string",
                "title": "Buyer key (Base Sepolia)",
                "description": "0x-prefixed private key of the testnet buyer wallet (scripts/setup_wallets.py makes one). "
                               "Stored by Claude Desktop as a secret and passed only to the local server.",
                "sensitive": True,
                "required": True,
            },
            "uv_path": {
                "type": "string",
                "title": "uv command",
                "description": "uv executable. Use the absolute path (run `which uv`) if Claude Desktop cannot find uv.",
                "default": "uv",
                "required": False,
            },
        },
    }


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--check", action="store_true", help="compare with --out instead of writing it")
    args = ap.parse_args(argv)

    doc = build()
    if find_key_like(doc):
        print("refusing to write: a string looks like a 64-hex private key", file=sys.stderr)
        return 2
    text = render(doc)
    if args.check:
        current = args.out.read_text() if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale; run: uv run python scripts/build_mcpb.py", file=sys.stderr)
            return 1
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(f"wrote {args.out} ({len(doc['tools'])} tools)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
