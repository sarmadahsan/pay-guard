from types import SimpleNamespace

from x402.http.types import HTTPRequestContext, HTTPTransportContext

from sellers.stall import make_stall_hook


class DummyAdapter:
    def get_header(self, name):
        return None

    def get_method(self):
        return "GET"

    def get_path(self):
        return "/"

    def get_url(self):
        return "http://seller.test/"

    def get_accept_header(self):
        return "*/*"


def ctx_for(path: str):
    req = HTTPRequestContext(adapter=DummyAdapter(), path=path, method="GET")
    return SimpleNamespace(transport_context=HTTPTransportContext(request=req), result=None)


async def test_hook_sleeps_only_for_research_slow():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    hook = make_stall_hook(stall_s=90, sleep=fake_sleep)
    await hook(ctx_for("/report/ACME"))
    await hook(ctx_for("/convert"))
    await hook(ctx_for("/report-premium/ACME"))
    assert slept == []
    await hook(ctx_for("/research-slow"))
    assert slept == [90]


async def test_hook_never_raises():
    async def boom(_s):
        raise RuntimeError("sleep exploded")

    hook = make_stall_hook(stall_s=1, sleep=boom)
    await hook(ctx_for("/research-slow"))  # swallowed
    await hook(SimpleNamespace(transport_context=None, result=None))  # no transport context: no-op
