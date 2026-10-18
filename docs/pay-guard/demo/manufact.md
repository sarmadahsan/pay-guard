# Publishing PayGuard on Manufact (Task 8.4)

**Status: BLOCKED (2026-10-17).** Manufact Cloud lists and serves only hosted MCP servers reached over an HTTPS URL. PayGuard is a stdio server that signs payments with keys from the operator's local `.env`. Task 8.4's rule applies: "If Manufact's flow needs a hosted (non-stdio) server, stop and report `BLOCKED`." Nothing was published. Everything that does not depend on the listing has shipped: `scripts/export_tool_schema.py`, the generated `mcp-tools.json`, `tests/test_tool_export.py`, and the README install section.

## Manifest format (from docs, 2026-10-17)

Sources read on 2026-10-17: `https://manufact.com`, `https://docs.manufact.com/llms.txt`, `https://docs.manufact.com/llms-full.txt`, and the pages `/dashboard/servers`, `/dashboard/connect-existing-server`, `/dashboard/build-runtimes`, `/dashboard/environment-variables`, `/dashboard/connect-clients`, `/dashboard/publish-checks`, `/dashboard/submission-pack`, `/cli/deploy`, `/cli/login`, plus `https://manufact.com/mcp-catalog`.

- **No manifest file.** Manufact (formerly mcp-use, YC S25) has no listing manifest such as `server.json` or `mcp.json`. A server is an object in Manufact Cloud, created from the dashboard (Servers, then New Server) or with `mcp-use deploy`. The CLI links a project to its cloud server through `.mcp-use/project.json`, which it writes itself.
- **Python is detected, not declared.** The build reads `pyproject.toml` or `requirements.txt` and supports uv, poetry, pipenv and pip. FastMCP is a detected framework preset. Anything else needs a committed `Dockerfile`.
- **Transport is HTTP only.** The connect-clients page says a deployed server "speaks MCP over HTTP at its gateway URL" (`https://<slug>.run.mcp-use.com/mcp`). `mcp-use deploy` takes `--port` (default 3000) and `--start-command`, so it expects a listening HTTP server. The two mentions of stdio in the full docs are about `mcp-use client connect --stdio`, a local terminal client, not a listing.
- **Env variables are set with values on the platform.** The Environment Variables tab, `mcp-use deploy --env KEY=VALUE`, `--env-file`, and `mcp-use servers env add` all store the value in Manufact Cloud for the running deployment. There is no way to declare a variable by name for the installing user to fill in.
- **The public catalog takes a URL.** `manufact.com/mcp-catalog` scores servers from "your MCP HTTPS URL". "Connect an existing server" also takes a full HTTPS MCP endpoint URL.

Why this blocks PayGuard rather than just needing a wrapper:

1. A hosted PayGuard would need `BUYER_KEY_SEPOLIA` (and `BUYER_KEY_MAINNET` for the Bazaar tool) stored in Manufact Cloud. That breaks the rule that keys stay in the operator's `.env` and that the listing names env variables but never holds values.
2. A hosted PayGuard is a shared buyer wallet that anyone with the gateway URL can spend from. That is a new payment path, which Task 8.4 forbids.
3. PayGuard has no HTTP transport today. Adding one is a server change outside 8.4's "new files only" scope.

## Publish command

From the docs, for an HTTP server (not run, for the reasons above):

```bash
npm install -g @mcp-use/cli
mcp-use login                      # OAuth device flow in a browser; or: mcp-use login --api-key <key> --org <org>
mcp-use deploy --runtime python --start-command "<command that serves MCP over HTTP>" --port 3000
```

After deploying, the dashboard's Publish section offers a Checklist ($0.10 per run; end-to-end ChatGPT and Claude checks on the Hobby plan, $2 per run) and a Submission pack ($5 per generation, Hobby plan) for app directories. Login is interactive, or needs an API key from the operator's Manufact account.

## Listing:

None. No Manufact listing exists for PayGuard, and none should be created with the current design.

The operator has three choices. Each one is a decision for the user, not a step to run blindly:

1. **Pitch without Manufact (recommended for 2026-10-18).** Use the README's "Install from Manufact" section as written. It shows the one-file install that Claude Desktop uses today, and `mcp-tools.json` as the advertised tool surface.
2. **Host a read-only demo listing on Manufact.** This would need an HTTP entry point that exposes only the admin tools with no wallet keys. It is a new task, because it changes the server and the listing would not be able to buy anything.
3. **List PayGuard where stdio servers are accepted.** The official MCP Registry (`server.json` with a PyPI package and stdio transport) and a Claude Desktop extension bundle (`.mcpb`, one-click install with user-entered secrets) both accept a local stdio server with env variables declared by name. Either would be a new task. The `.mcpb` bundle now exists: docs/pay-guard/demo/mcpb.md.

If the user still picks a hosted Manufact deploy, the remaining steps start with `mcp-use login`, which needs the operator's account.

## Verified install

Not run: there is no listing to install from. When a listing exists, run this check on a clean Claude Desktop config:

1. Install PayGuard from the listing, fill the operator's `.env` (names in `mcp-tools.json` under `env`), and restart Claude Desktop.
2. Ask Claude to run `list_services`. It must return the allowlisted services.
3. Compare the tool names Claude Desktop shows for PayGuard with `mcp-tools.json`:

   ```bash
   python3 -c "import json; print(sorted(t['name'] for t in json.load(open('mcp-tools.json'))['tools']))"
   ```

   With `INTEL_BASE_URL` and `BAZAAR_URL` unset the install shows 9 tools. Each gated tool appears only when its env is set.
4. Confirm that no key value appears anywhere in the listing's page, its config snippet, or this repository:

   ```bash
   git grep -nE '0x[0-9a-fA-F]{64}'
   ```

   Expected: no output. The listing must show env variable names only.

`scripts/export_tool_schema.py` refuses to write `mcp-tools.json` if any string in it looks like a 64-hex key, and `tests/test_tool_export.py` fails if the committed file is stale or advertises an `approve_retry` tool or a force or retry parameter.
