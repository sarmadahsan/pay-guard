# Claude Desktop extension (.mcpb)

**Status: manifest committed and validated (2026-10-17).** `mcpb/manifest.json` installs PayGuard in Claude Desktop in one click. The bundle holds only the manifest: Claude Desktop runs the unchanged stdio server from the operator's own checkout, and the buyer key is typed into Claude Desktop's settings form, never into a file in this repo. This replaces the blocked Manufact listing (docs/pay-guard/demo/manufact.md) for the pitch.

## Spec (read 2026-10-17)

Sources: `https://github.com/modelcontextprotocol/mcpb` at commit `70fe3b3` (2026-04-22), its `MANIFEST.md` ("Current version: `0.3`, Last updated: 2025-12-02"), and the zod schemas shipped in `@anthropic-ai/mcpb@2.1.2` (latest on npm that day).

- **Bundle.** A `.mcpb` file is a zip with `manifest.json` at the root plus whatever the server needs. `mcpb pack <dir>` builds it; `mcpb validate <manifest>` checks the schema.
- **Required fields.** `manifest_version`, `name`, `version`, `description`, `author.name`, `server`. The 0.3 schema is strict: unknown keys fail validation.
- **`server`.** `type` is `python`, `node` or `binary` (0.4 adds `uv`, where the host installs dependencies from a bundled `pyproject.toml`). `entry_point` is a string; the validator does not check that the file exists. `mcp_config` is `{command, args, env, platform_overrides}` and is what the client actually runs.
- **Substitution in `mcp_config`.** `${__dirname}` (the unpacked bundle), `${HOME}`, `${DESKTOP}`, `${DOCUMENTS}`, `${DOWNLOADS}`, `${/}`, and `${user_config.KEY}`.
- **`user_config`.** A map of fields the client shows as a form at install time. Each has `type` (`string`, `number`, `boolean`, `directory`, `file`), `title`, `description`, optional `required`, `default`, `multiple`, `min`/`max`, and `sensitive`, which for strings means "mask input and store securely". The spec recommends passing secrets through `env`.
- **`tools` / `tools_generated`.** A static list of `{name, description}`. `tools_generated: true` would mean the server can add tools beyond the list.

## How PayGuard uses it

| Manifest part | Value | Why |
| --- | --- | --- |
| `manifest_version` | `0.3` | The version `MANIFEST.md` calls current. The 0.4 `uv` type would copy the server into the bundle and run it away from the checkout's `.env`, `policy.yaml` and ledger. |
| `server.mcp_config.command` / `args` | `${user_config.uv_path} run --directory ${user_config.repo_dir} python -m payguard.server` | The same command as `mcp-tools.json` and the README, with `<repo>` filled from a directory picker. No server code changes. |
| `server.type` / `entry_point` | `python`, `payguard/server.py` | Nominal (the schema requires them). The bundle ships no Python; `mcp_config` decides what runs. |
| `server.mcp_config.env` | only `BUYER_KEY_SEPOLIA` = `${user_config.buyer_key_sepolia}` | The one secret entered in Claude Desktop. `load_dotenv` does not override variables already set, so this value wins over a key in `.env`. |
| `user_config.buyer_key_sepolia` | `string`, `sensitive`, `required`, no default | Masked and stored by the client; never written to the repo. |
| `user_config.repo_dir` | `directory`, `required` | The operator's clone, after `uv sync` and filling `.env`. |
| `user_config.uv_path` | `string`, default `uv` | Claude Desktop may start servers without the shell's `PATH`; set the absolute path from `which uv` if it cannot find uv. |
| other env | named in `long_description` only | `SELLER_BASE_URL`, `RPC_URL_SEPOLIA` and the optional ones stay in `<repo>/.env`. Passing them as optional `user_config` fields would set them to empty strings when left blank, which would mask the `.env` values. |
| `tools`, `tools_generated: false` | the 11 tools of `mcp-tools.json`, name and description | Generated from `mcp-tools.json` so the two cannot drift. |

`get_intel_report` (needs `INTEL_BASE_URL`) and `get_public_data` (needs `BAZAAR_URL` and `BAZAAR_PRICE_MINOR`) are listed because they are part of the advertised surface, but the server registers them only when their env is set in `.env`. With both unset Claude Desktop shows 9 tools.

## Build and check

```bash
uv run python scripts/export_tool_schema.py          # mcp-tools.json, if a tool changed
uv run python scripts/build_mcpb.py                  # mcpb/manifest.json from mcp-tools.json
npx -y @anthropic-ai/mcpb@2.1.2 validate mcpb/manifest.json
npx -y @anthropic-ai/mcpb@2.1.2 pack mcpb dist/payguard.mcpb
```

`scripts/build_mcpb.py` refuses to write a manifest containing a 64-hex string. `tests/test_mcpb_manifest.py` fails if the manifest is stale, lists different tools than `mcp-tools.json`, launches anything other than `payguard.server`, or passes any env value other than the sensitive buyer key. `dist/` is git-ignored; attach `payguard.mcpb` to a GitHub release to hand it out.

Checked on 2026-10-17: `mcpb validate` passes; `mcpb pack` produces a 2.1 kB bundle with only `manifest.json`; running the manifest's command with the placeholders filled (a throwaway key generated in memory, no `.env`) answers `initialize` as `payguard` and lists the 9 ungated tools.

## Verified install

Not yet run in Claude Desktop. On the demo machine:

1. Double-click `dist/payguard.mcpb` (or drag it onto Claude Desktop's Settings, Extensions). Pick the pay-guard checkout, paste the Sepolia buyer key, and leave `uv command` as `uv` unless the extension fails to start.
2. Ask Claude to run `list_services`. It must return the allowlisted services.
3. Compare the tool names Claude Desktop shows with `mcp-tools.json` (9 without `INTEL_BASE_URL` and `BAZAAR_URL`, 11 with both).
4. Remove any old `payguard` entry from `claude_desktop_config.json` first, or Claude Desktop runs two copies of the server against the same ledger.
5. `git grep -nE '0x[0-9a-fA-F]{64}'` prints nothing.
