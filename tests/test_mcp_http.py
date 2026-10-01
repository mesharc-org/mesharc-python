"""The hosted MCP server: whose credential a request acts with, and what the
transport admits.

The first test in this file is the one that matters most. In hosted mode the
server holds no key of its own, and every request acts as the caller who sent
it. If MESHARC_API_KEY were ever read there, every caller would act inside
whichever workspace the operator's key belongs to -- reading other tenants'
pages and spending their credits. `MeshArc.__init__` falls back to that
variable on its own, so it is not enough for `_client()` to avoid naming it:
it has to refuse to build a client at all without a caller's token.
"""
import os

import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")

from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

RESOURCE = "http://mcp.example.test/mcp"


class _User(AuthenticatedUser):
    def __init__(self, at):
        self.access_token = at


def _answers(body):
    """An async stub for the verifier's introspection call."""
    async def _ask(_token):
        return body
    return _ask


def _as(token, grant="g1", scopes=("read",)):
    at = AccessToken(token=token, client_id=grant, scopes=list(scopes),
                     resource=RESOURCE, claims={"grant": grant, "org": "o1"})
    auth_context_var.set(_User(at))
    return at


@pytest.fixture(autouse=True)
def _hosted(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_HTTP", True)
    mcp_mod._CLIENTS.clear()
    auth_context_var.set(None)
    yield
    mcp_mod._CLIENTS.clear()
    auth_context_var.set(None)


def test_no_caller_token_never_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("MESHARC_API_KEY", "mesharc_operator_key")
    auth_context_var.set(None)
    with pytest.raises(RuntimeError):
        mcp_mod._client()
    assert not mcp_mod._CLIENTS, "a request with no token must not leave a client behind"


def test_each_caller_acts_with_its_own_token(monkeypatch):
    monkeypatch.setenv("MESHARC_API_KEY", "mesharc_operator_key")
    sent = {}
    for token in ("mesharc_oat_a", "mesharc_oat_b"):
        _as(token, grant="g-" + token[-1])
        client = mcp_mod._client()
        sent[token] = client._h._c.headers.get("Authorization")
    assert sent == {"mesharc_oat_a": "Bearer mesharc_oat_a",
                    "mesharc_oat_b": "Bearer mesharc_oat_b"}
    assert not any("operator" in v for v in sent.values())
    assert len(mcp_mod._CLIENTS) == 2, "one client per caller, not one shared"


def test_the_same_caller_reuses_one_client():
    _as("mesharc_oat_a")
    first = mcp_mod._client()
    _as("mesharc_oat_a")
    assert mcp_mod._client() is first, "the rate-limit memory has to outlive one call"


def test_the_client_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_CLIENTS_MAX", 4)
    for i in range(10):
        _as(f"mesharc_oat_{i}", grant=f"g{i}")
        mcp_mod._client()
    assert len(mcp_mod._CLIENTS) == 4


def test_idempotency_keys_belong_to_the_grant():
    _as("mesharc_oat_a", grant="grant-one")
    one = mcp_mod._key("scrape", ["https://x.test/"])
    _as("mesharc_oat_b", grant="grant-two")
    two = mcp_mod._key("scrape", ["https://x.test/"])
    assert one != two, "two workspaces asking for the same page are two jobs"
    assert "grant-one" in one and "grant-two" in two


def test_stdio_is_unchanged(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_HTTP", False)
    monkeypatch.setenv("MESHARC_API_KEY", "mesharc_local_key")
    monkeypatch.setattr(mcp_mod, "_CLIENT", None)
    client = mcp_mod._client()
    assert client._h._c.headers.get("Authorization") == "Bearer mesharc_local_key"
    assert mcp_mod._budget() is None, "a local caller waits as long as it likes"


def test_hosted_waits_are_bounded(monkeypatch):
    monkeypatch.delenv("MESHARC_MCP_WAIT", raising=False)
    assert mcp_mod._budget() == mcp_mod.WAIT_DEFAULT_S
    monkeypatch.setenv("MESHARC_MCP_WAIT", "9")
    assert mcp_mod._budget() == 9.0
    monkeypatch.setenv("MESHARC_MCP_WAIT", "not-a-number")
    assert mcp_mod._budget() == mcp_mod.WAIT_DEFAULT_S, "a bad value must not mean unbounded"


def test_a_job_still_running_says_so_and_names_itself():
    out = mcp_mod._still_running("crawl", "c1", {"pages": 3})
    assert out["status"] == "running"
    assert out["job"] == {"kind": "crawl", "id": "c1"}
    assert "get_job" in out["note"], "an assistant told only 'running' would start the work again"
    assert "keeps running" in out["note"]


def test_the_transport_admits_the_public_host_and_nothing_else():
    """Binding 127.0.0.1 makes the SDK turn on a localhost-only Host
    allow-list by itself, so behind a proxy every request would be refused.
    These settings are what stop that."""
    s = mcp_mod._transport_security("https://mcp.example.test/mcp")
    assert s.enable_dns_rebinding_protection is True
    assert "mcp.example.test" in s.allowed_hosts
    assert "127.0.0.1:*" in s.allowed_hosts, "the inspector still has to reach it locally"
    assert "https://mcp.example.test" in s.allowed_origins
    assert "evil.example" not in s.allowed_hosts


def test_extra_origins_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MESHARC_MCP_ALLOWED_ORIGINS", "https://a.test, https://b.test")
    s = mcp_mod._transport_security("https://mcp.example.test/mcp")
    assert "https://a.test" in s.allowed_origins and "https://b.test" in s.allowed_origins


def test_the_verifier_fails_closed(monkeypatch):
    """A check that could not be made is a refusal. There is no branch that
    admits a request because introspection was unreachable."""
    import anyio

    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "s3cret")

    for boom in (TimeoutError("slow"), ValueError("not json"), RuntimeError("500")):
        async def raises(_t, e=boom):
            raise e
        monkeypatch.setattr(v, "_ask", raises)
        assert anyio.run(v.verify_token, "mesharc_oat_x") is None

    monkeypatch.setattr(v, "_ask", _answers({"active": False}))
    assert anyio.run(v.verify_token, "mesharc_oat_x") is None
    monkeypatch.setattr(v, "_ask", _answers("a string, not a body"))
    assert anyio.run(v.verify_token, "mesharc_oat_x") is None


def test_an_active_token_carries_its_grant_and_audience(monkeypatch):
    import anyio

    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "s3cret")
    monkeypatch.setattr(v, "_ask", _answers({
        "active": True, "scope": "read write", "client_id": "grant-9",
        "org": "org-9", "exp": 4000000000, "aud": RESOURCE}))
    at = anyio.run(v.verify_token, "mesharc_oat_x")
    assert at.scopes == ["read", "write"]
    assert at.resource == RESOURCE, "the SDK compares this against the public url"
    assert at.claims["grant"] == "grant-9" and at.claims["org"] == "org-9"


def test_the_verifier_caches_a_live_answer_only(monkeypatch):
    import anyio

    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "s3cret")
    calls = []

    async def ask(_t):
        calls.append(1)
        return {"active": True, "scope": "read", "client_id": "g", "exp": 4000000000, "aud": RESOURCE}

    monkeypatch.setattr(v, "_ask", ask)
    anyio.run(v.verify_token, "mesharc_oat_x")
    anyio.run(v.verify_token, "mesharc_oat_x")
    assert len(calls) == 1, "a cold call costs one hop; a warm one costs none"

    # An inactive answer must not be cached, or revoking a token would be
    # undone for a minute by the first caller who tried it while it was live.
    refusals = []

    async def refuse(_t):
        refusals.append(1)
        return {"active": False}

    monkeypatch.setattr(v, "_ask", refuse)
    anyio.run(v.verify_token, "mesharc_oat_never_seen")
    anyio.run(v.verify_token, "mesharc_oat_never_seen")
    assert len(refusals) == 2, "every refusal is asked again, never served from the cache"


def test_http_mode_needs_its_environment(monkeypatch):
    for name in ("MESHARC_MCP_PUBLIC_URL", "MESHARC_OAUTH_ISSUER", "MESHARC_INTROSPECT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.argv", ["mesharc-mcp", "--http"])
    with pytest.raises(SystemExit) as exc:
        mcp_mod.main()
    assert "MESHARC_MCP_PUBLIC_URL" in str(exc.value)
    assert "MESHARC_INTROSPECT_SECRET" in str(exc.value)


def test_a_bad_secret_stops_the_server_before_it_serves(monkeypatch):
    """A wrong secret makes every token look invalid, so without this the
    service starts clean and then 401s everything with nothing naming why."""
    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "wrong")

    class R:
        status_code = 401

    monkeypatch.setattr("httpx.post", lambda *a, **k: R())
    with pytest.raises(SystemExit) as exc:
        mcp_mod._check_secret(v)
    assert "MESHARC_INTROSPECT_SECRET" in str(exc.value)


def test_an_unreachable_issuer_stops_the_server(monkeypatch):
    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "s3cret")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.post", boom)
    with pytest.raises(SystemExit) as exc:
        mcp_mod._check_secret(v)
    assert "MESHARC_OAUTH_ISSUER" in str(exc.value)


def test_get_job_is_offered_alongside_the_rest():
    source = (os.path.dirname(mcp_mod.__file__) + os.sep + "mcp.py")
    text = open(source, encoding="utf-8").read()
    assert text.count("@server.tool") == 17, "sixteen verbs plus get_job"
    assert "def get_job(" in text


def test_a_finished_crawl_is_finished_however_it_finished():
    """The API sends `error`, which an allow-list of terminal names missed --
    so a failed crawl was reported as still running and polled for ever."""
    from mesharc import _running
    for status in ("done", "complete", "failed", "cancelled", "error", "stopped", "whatever-is-added-next"):
        assert not _running(status), f"{status} is not a job still going"
    for status in ("queued", "running"):
        assert _running(status)


def test_only_the_cap_is_ever_fetched():
    """`pages()` walks the whole crawl in batches. Materialising it and slicing
    afterwards fetched every page to keep fifty."""
    from itertools import islice

    asked = []

    def pages(limit=25, **_kw):
        for i in range(10_000):
            asked.append(i)
            yield {"url": f"https://x.test/{i}"}

    kept = list(islice(pages(limit=mcp_mod.PAGES_CAP), mcp_mod.PAGES_CAP))
    assert len(kept) == mcp_mod.PAGES_CAP
    assert len(asked) == mcp_mod.PAGES_CAP, "a page not returned should not have been fetched"


def test_the_introspection_call_does_not_block_the_loop():
    """verify_token runs on the event loop. A blocking client there held every
    other request for the timeout on each token it had not seen."""
    import inspect

    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "s")
    assert inspect.iscoroutinefunction(v._ask), "introspection has to be awaited, not blocked on"


def test_a_token_with_no_audience_is_refused(monkeypatch):
    """Substituting this server's own URL for a missing `aud` made the SDK
    compare the value against itself, so the audience check -- the whole thing
    stopping a token minted elsewhere being spent here -- always passed."""
    import anyio

    v = mcp_mod._IntrospectionVerifier("http://api.test", RESOURCE, "s3cret")
    monkeypatch.setattr(v, "_ask", _answers({
        "active": True, "scope": "read", "client_id": "g", "exp": 4000000000}))
    assert anyio.run(v.verify_token, "mesharc_oat_x") is None

    monkeypatch.setattr(v, "_ask", _answers({
        "active": True, "scope": "read", "client_id": "g", "exp": 4000000000,
        "aud": "https://somewhere-else.test/mcp"}))
    at = anyio.run(v.verify_token, "mesharc_oat_y")
    assert at is not None and at.resource == "https://somewhere-else.test/mcp", (
        "another resource's audience is carried through for the SDK to reject, not rewritten")
