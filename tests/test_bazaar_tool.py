"""Task 7.1: the env-gated fourth paid tool (public Bazaar seller on Base mainnet)."""
import importlib
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

BAZAAR_VARS = ("BAZAAR_URL", "BAZAAR_PAY_TO", "BAZAAR_PRICE_MINOR", "BAZAAR_CONTENT_TYPE", "BAZAAR_TOOL_NAME")
TEST_URL = "https://example.invalid/x"
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DOTENV_BAZAAR = {k: v for k, v in (dotenv_values(_ENV_FILE) if _ENV_FILE.exists() else {}).items() if k in BAZAAR_VARS and v}

# Hermetic against an operator .env: payguard.server runs load_dotenv(.env) on import and that would register the
# bazaar tool for every later test module (test_presentation, test_response_stats and test_schema sort after this
# file and bind `mcp` at collection; no conftest edit is allowed here). Only BAZAAR_URL is blanked: the registry
# treats empty as unset, load_dotenv never overrides an existing var, and the other vars are inert without a URL
# (blanking BAZAAR_TOOL_NAME / BAZAAR_CONTENT_TYPE would instead become an empty name / content type).
# A BAZAAR_URL exported by the caller is left alone.
if "BAZAAR_URL" not in os.environ:
    os.environ["BAZAAR_URL"] = ""

import payguard.server as _server_mod  # noqa: E402,F401


def _reload_server():
    import payguard.server as srv

    return importlib.reload(srv)


@pytest.fixture
def reload_server(monkeypatch):
    """Reload payguard.server under the current env; always leave sys.modules in the no-bazaar state."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # reloads must not re-read an operator .env
    yield _reload_server
    for var in BAZAAR_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BAZAAR_URL", "")
    _reload_server()


async def _tools(srv):
    return {t.name: t for t in await srv.mcp.list_tools()}


@pytest.mark.skipif(not (os.environ.get("BAZAAR_URL") or DOTENV_BAZAAR.get("BAZAAR_URL")), reason="no Bazaar config in env or .env")
async def test_real_env_registers_bazaar_tool(monkeypatch, reload_server):
    for k, v in DOTENV_BAZAAR.items():
        if not os.environ.get(k):  # process env wins over .env
            monkeypatch.setenv(k, v)
    name = os.environ.get("BAZAAR_TOOL_NAME") or "get_public_data"
    srv = reload_server()
    tools = await _tools(srv)
    try:
        price_ok = int(os.environ.get("BAZAAR_PRICE_MINOR", "")) > 0  # same parse as load_registry()
    except ValueError:
        price_ok = False
    name_ok = (bool(srv._BAZAAR_NAME_OK.fullmatch(name)) and not any(w in name.lower() for w in srv._BAZAAR_NAME_BANNED)
               and name not in vars(srv))  # same rules as _register_bazaar_tool()
    if not (price_ok and name_ok):  # refused: no mainnet tool under that name (it may still be another real tool)
        assert name not in tools or "mainnet" not in tools[name].description
        return
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
    assert "$0.001 USDC" in tool.description


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


@pytest.mark.parametrize("price,text", [("1000", "$0.001 USDC"), ("10000", "$0.01 USDC"), ("500000", "$0.50 USDC"),
                                        ("1234567", "$1.234567 USDC"), ("2000000", "$2.00 USDC")])
async def test_price_text_is_trimmed_integer_math(monkeypatch, reload_server, price, text):
    monkeypatch.setenv("BAZAAR_URL", TEST_URL)
    monkeypatch.setenv("BAZAAR_PRICE_MINOR", price)
    monkeypatch.delenv("BAZAAR_TOOL_NAME", raising=False)
    assert text in (await _tools(reload_server()))["get_public_data"].description


@pytest.mark.parametrize("name", ["approve_it", "force_buy", "retry_data", "Get_Retry", "Bad-Name", "1abc", "has space", "Upper", "get_x\n", "get_company_report", "list_services", "mcp"])
async def test_unsafe_tool_name_refused(monkeypatch, reload_server, name):
    monkeypatch.setenv("BAZAAR_URL", "")
    baseline = set(await _tools(reload_server()))
    monkeypatch.setenv("BAZAAR_URL", TEST_URL)
    monkeypatch.setenv("BAZAAR_PRICE_MINOR", "1000")
    monkeypatch.setenv("BAZAAR_TOOL_NAME", name)
    tools = await _tools(reload_server())
    assert set(tools) == baseline and "get_public_data" not in tools
    assert name not in tools or "mainnet" not in tools[name].description  # a taken name keeps its original tool
