"""Task 7.1: the env-gated fourth paid tool (public Bazaar seller on Base mainnet)."""
import importlib
import os

import pytest

import payguard.server as _server_mod  # noqa: F401  (ensures the module is importable before reloads)

BAZAAR_VARS = ("BAZAAR_URL", "BAZAAR_PAY_TO", "BAZAAR_PRICE_MINOR", "BAZAAR_CONTENT_TYPE", "BAZAAR_TOOL_NAME")
TEST_URL = "https://example.invalid/x"


def _reload_server():
    import payguard.server as srv

    return importlib.reload(srv)


@pytest.fixture
def reload_server(monkeypatch):
    """Reload payguard.server under the current env; always leave sys.modules in the no-bazaar state."""
    yield _reload_server
    for var in BAZAAR_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BAZAAR_URL", "")  # empty == unset for the registry; load_dotenv will not override it
    _reload_server()


async def _tools(srv):
    return {t.name: t for t in await srv.mcp.list_tools()}


@pytest.mark.skipif(not os.environ.get("BAZAAR_URL"), reason="BAZAAR_URL not set")
async def test_real_env_registers_bazaar_tool(reload_server):
    name = os.environ.get("BAZAAR_TOOL_NAME", "get_public_data")
    tools = await _tools(reload_server())
    assert name in tools
    assert set(tools[name].parameters["properties"]) == {"purchase_intent_id", "task_id"}


async def test_bazaar_tool_registered_with_exact_params(monkeypatch, reload_server):
    monkeypatch.setenv("BAZAAR_URL", TEST_URL)
    monkeypatch.setenv("BAZAAR_PRICE_MINOR", "1000")
    monkeypatch.delenv("BAZAAR_TOOL_NAME", raising=False)
    tool = (await _tools(reload_server()))["get_public_data"]
    props = set(tool.parameters["properties"])
    assert props == {"purchase_intent_id", "task_id"}
    assert not any("force" in p.lower() or "retry" in p.lower() for p in props)
    assert "mainnet" in tool.description
    assert "0.001" in tool.description


async def test_custom_tool_name_honored(monkeypatch, reload_server):
    monkeypatch.setenv("BAZAAR_URL", TEST_URL)
    monkeypatch.setenv("BAZAAR_PRICE_MINOR", "10000")
    monkeypatch.setenv("BAZAAR_TOOL_NAME", "get_weather_data")
    tools = await _tools(reload_server())
    assert "get_weather_data" in tools and "get_public_data" not in tools


@pytest.mark.parametrize("price", [None, "abc", "0", "-5"])
async def test_no_tool_when_url_empty_or_price_invalid(monkeypatch, reload_server, price):
    monkeypatch.delenv("BAZAAR_TOOL_NAME", raising=False)
    monkeypatch.setenv("BAZAAR_URL", "")
    if price is not None:
        monkeypatch.setenv("BAZAAR_PRICE_MINOR", price)
    assert "get_public_data" not in await _tools(reload_server())

    monkeypatch.setenv("BAZAAR_URL", TEST_URL)  # URL set, price missing/invalid: imports fine, tool skipped
    if price is None:
        monkeypatch.delenv("BAZAAR_PRICE_MINOR", raising=False)
    assert "get_public_data" not in await _tools(reload_server())


async def test_tool_routes_to_bazaar_service(monkeypatch, reload_server):
    monkeypatch.setenv("BAZAAR_URL", TEST_URL)
    monkeypatch.setenv("BAZAAR_PRICE_MINOR", "1000")
    monkeypatch.delenv("BAZAAR_TOOL_NAME", raising=False)
    srv = reload_server()
    calls = []

    async def fake_purchase(deps, service_id, args, intent_id, task_id):
        calls.append((deps, service_id, args, intent_id, task_id))
        return {"ok": True}

    monkeypatch.setattr(srv, "purchase", fake_purchase)
    monkeypatch.setattr(srv, "get_deps", lambda: "DEPS")
    await srv.mcp.call_tool("get_public_data", {"purchase_intent_id": "pi-1", "task_id": "t-1"})
    assert calls == [("DEPS", "bazaar", {}, "pi-1", "t-1")]
