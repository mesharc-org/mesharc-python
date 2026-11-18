"""16 Nov 2026: a search that one dropped connection failed, news search,
a picked pair of runs explained, and a screenshot's cost as the API says it.

search_web sent no idempotency key, so the SDK -- which repeats a POST only
with one -- gave up on the first network error. It now sends a fresh key per
call: one call is safe to retry, two calls are two searches, and a retry that
finds the first request still being answered (409 in_flight) asks again with
the same key -- the first answer replayed, free -- rather than a new search.
"""
import json

import httpx
import pytest

import mesharc
from mesharc import MeshArcError

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")


IN_FLIGHT = MeshArcError(409, "a request with this Idempotency-Key is still running", "in_flight")


class _Searches:
    """A client for search_web: records each call; answers, or raises each
    of `raises` in turn before answering (all of them for ever with
    `always`). `listed` is what the workspace's search list holds."""

    def __init__(self, answer=None, raises=None, always=False, listed=()):
        self.answer, self.always, self.calls, self.listed = answer, always, [], listed
        self.raises = list(raises) if isinstance(raises, (list, tuple)) else ([raises] if raises else [])

    def web_search(self, query, **kw):
        self.calls.append((query, kw))
        if self.raises:
            exc = self.raises[0] if self.always else self.raises.pop(0)
            raise exc
        return self.answer

    def searches(self, q=None, limit=25):
        if isinstance(self.listed, Exception):
            raise self.listed
        yield from self.listed

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


@pytest.fixture
def no_sleep(monkeypatch):
    """Pauses between asks, recorded rather than slept."""
    slept = []
    monkeypatch.setattr(mcp_mod.time, "sleep", slept.append)
    return slept


def _done(**extra):
    return {"id": "s1", "status": "done", "query": "q", "engine": "google", "cached": False, "creditsUsed": 4,
            "data": [{"position": 1, "title": "T", "url": "https://x.test/", "snippet": "s", "source": "web",
                      "engine": "google"}], **extra}


@pytest.fixture(params=[None, 25.0], ids=["stdio", "hosted"])
def budget(request, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_budget", lambda: request.param)
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read", "write"])
    return request.param


# --- search_web: a key per call, and in_flight answered --------------------

def test_each_search_sends_a_key_of_its_own(monkeypatch, budget):
    arc = _Searches(_done())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    mcp_mod.search_web("q")
    mcp_mod.search_web("q")
    (_, first), (_, second) = arc.calls
    assert first["idempotency_key"].startswith("mcp-search-")
    assert second["idempotency_key"].startswith("mcp-search-")
    assert first["idempotency_key"] != second["idempotency_key"], "two calls are two searches, not one replayed"


@pytest.mark.parametrize("detail,code", [
    ("a request with this Idempotency-Key is still running", "in_flight"),
    # An API from before the code said so only in the message.
    ("a request with this Idempotency-Key is still running", "conflict"),
])
def test_a_search_still_in_flight_is_asked_again_with_the_same_key(monkeypatch, budget, no_sleep, detail, code):
    """The same key replays the first answer, free, however that search
    ended; an equal search may not be served from the cache (one that ran
    out of time is skipped), so it is never what the tool falls back to."""
    busy = MeshArcError(409, detail, code)
    arc = _Searches(_done(), raises=[busy, busy])
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.search_web("q")
    assert out["status"] == "done" and out["results"][0]["url"] == "https://x.test/"
    keys = {kw["idempotency_key"] for _, kw in arc.calls}
    assert len(arc.calls) == 3 and len(keys) == 1, "asked three times, one search"
    assert no_sleep == [mcp_mod.SEARCH_ASK_PAUSE_S] * 2


def _still_busy(monkeypatch, budget, listed):
    """A search still in flight when the call must answer: no time left for
    another ask, run locally or hosted."""
    if budget is None:
        monkeypatch.setattr(mcp_mod, "SEARCH_REPLAY_S", 0.0)
    else:
        monkeypatch.setattr(mcp_mod, "_budget", lambda: 10.0)
    arc = _Searches(raises=IN_FLIGHT, always=True, listed=listed)
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    return mcp_mod.search_web("Acme  news"), arc


def test_a_search_still_in_flight_at_the_end_is_handed_on_as_a_job(monkeypatch, budget, no_sleep):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    listed = [{"id": "other", "query": "something else", "createdAt": now.isoformat()},
              {"id": "s7", "query": "acme news", "status": "running", "createdAt": now.isoformat()},
              {"id": "old", "query": "acme news", "createdAt": (now - timedelta(hours=2)).isoformat()}]
    out, arc = _still_busy(monkeypatch, budget, listed)
    assert out["status"] == "running" and out["job"] == {"kind": "search", "id": "s7"}
    assert len(arc.calls) == 1 and no_sleep == []


@pytest.mark.parametrize("listed", [[], MeshArcError(503, "down", "unavailable")], ids=["none", "unreadable"])
def test_a_search_that_cannot_be_found_says_so_and_promises_nothing_free(monkeypatch, budget, no_sleep, listed):
    from datetime import datetime, timedelta, timezone
    if listed == []:
        listed = [{"id": "old", "query": "acme news",
                   "createdAt": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}]
    out, _ = _still_busy(monkeypatch, budget, listed)
    assert out == {"status": "running", "note": mcp_mod.SEARCH_ACCEPTING}
    note = out["note"]
    assert "no charge" not in note and "free" not in note and "cache" not in note
    assert "409" not in json.dumps(out)


def test_any_other_409_is_passed_on(monkeypatch, budget):
    exc = MeshArcError(409, "something else", "conflict", "req_1")
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Searches(raises=exc))
    out = mcp_mod.search_web("q")
    assert out["status"] == 409 and out["code"] == "conflict" and out["request_id"] == "req_1"


def test_the_sdk_retries_a_dropped_search_with_the_same_key(monkeypatch):
    """The transport end to end, with no network: a connection dropped once,
    then answered. The retry carries the first request's key."""
    keys = []

    def handle(request):
        keys.append(request.headers.get("Idempotency-Key"))
        if len(keys) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json=_done())

    arc = mesharc.MeshArc("mesharc_test", base_url="https://api.test", max_retries=2)
    arc._h._c = httpx.Client(base_url="https://api.test/api/v1", transport=httpx.MockTransport(handle))
    arc._h._backoff = lambda attempt: 0  # type: ignore[method-assign]
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.search_web("q")
    assert out["status"] == "done" and out["results"][0]["url"] == "https://x.test/"
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0].startswith("mcp-search-")


def _arc_answering(handle):
    arc = mesharc.MeshArc("mesharc_test", base_url="https://api.test", max_retries=2)
    arc._h._c = httpx.Client(base_url="https://api.test/api/v1", transport=httpx.MockTransport(handle))
    return arc


def test_a_hosted_search_does_not_sleep_out_a_long_retry_after(monkeypatch):
    """The key makes the POST repeatable, and the client sleeps a 429's
    Retry-After: 55 s twice ran a hosted call far past its client's wait.
    Hosted, a retry that would end past the budget is not made."""
    slept = []
    monkeypatch.setattr(mesharc.time, "sleep", slept.append)
    monkeypatch.setattr(mcp_mod, "_budget", lambda: 25.0)
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read", "write"])
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(429, headers={"Retry-After": "55"}, json={"error": "slow down", "code": "rate_limited"})

    monkeypatch.setattr(mcp_mod, "_client", lambda: _arc_answering(handle))
    out = mcp_mod.search_web("q")
    assert out["status"] == 429 and out["code"] == "rate_limited"
    assert sum(slept) <= 25.0 and len(sent) == 1

    # A short wait that fits is still retried.
    slept.clear()
    sent.clear()
    replies = [httpx.Response(503, headers={"Retry-After": "2"}), httpx.Response(200, json=_done())]
    monkeypatch.setattr(mcp_mod, "_client", lambda: _arc_answering(lambda r: (sent.append(r), replies.pop(0))[1]))
    assert mcp_mod.search_web("q")["status"] == "done"
    assert slept == [2.0] and len(sent) == 2


def test_run_locally_a_search_still_waits_out_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(mesharc.time, "sleep", slept.append)
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    replies = [httpx.Response(429, headers={"Retry-After": "55"}), httpx.Response(200, json=_done())]
    monkeypatch.setattr(mcp_mod, "_client", lambda: _arc_answering(lambda r: replies.pop(0)))
    assert mcp_mod.search_web("q")["status"] == "done"
    assert slept == [55.0], "stdio waits as it always has; only hosted has a budget to keep"


# --- search_web: news ------------------------------------------------------

def test_source_news_reaches_the_request(monkeypatch):
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_done(data=[{
            "position": 1, "title": "Acme raises", "url": "https://news.test/a", "snippet": "s",
            "source": "news", "engine": "google", "publisher": "The Paper", "age": "20h"}]))

    arc = mesharc.MeshArc("mesharc_test", base_url="https://api.test")
    arc._h._c = httpx.Client(base_url="https://api.test/api/v1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.search_web("acme", source="news")
    assert bodies[0]["sources"] == ["news"]
    assert out["results"][0]["publisher"] == "The Paper" and out["results"][0]["age"] == "20h"

    bodies.clear()
    arc = mesharc.MeshArc("mesharc_test", base_url="https://api.test")
    arc._h._c = httpx.Client(base_url="https://api.test/api/v1", transport=httpx.MockTransport(
        lambda r: (bodies.append(json.loads(r.content)), httpx.Response(200, json=_done()))[1]))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.search_web("acme")
    assert "sources" not in bodies[0], "web is the API's default, and is not sent"
    assert "publisher" not in out["results"][0] and "age" not in out["results"][0]


def test_source_is_a_fixed_choice():
    import asyncio
    tool = {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}["search_web"]
    schema = tool.input_schema["properties"]["source"]
    assert schema["enum"] == ["web", "news"] and schema["default"] == "web"
    assert "publisher" in tool.description and "ten minutes" in tool.description


# --- get_changes: a picked pair, explained ---------------------------------

class _Changes:
    def __init__(self, change):
        self.change = change

    def changes(self, project_id, run_id=None, against=None):
        return {"change": self.change, "runs": []}


def _row(kind="modified", **extra):
    return {"url": "https://x.test/a", "path": "/a", "kind": kind, **extra}


def _changes(monkeypatch, change):
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Changes(change))
    return mcp_mod.get_changes("p1", "r2", against="r1")["change"]


def test_an_older_apis_pair_across_rules_is_explained(monkeypatch):
    ch = _changes(monkeypatch, {"adHoc": True, "crossVersion": [3, 4], "readDifferentlyCount": 0,
                                "feed": [_row(added="+0", removed="− 0", addedN=0, removedN=0)]})
    assert mcp_mod.PAIR_RULES in ch["note"] and mcp_mod.PAIR_UNCOUNTED in ch["note"]
    assert ch["feed"][0]["addedN"] == 0, "the API's numbers are left as sent"


def test_an_older_apis_pair_read_alike_says_its_zeros_are_not_counts(monkeypatch):
    ch = _changes(monkeypatch, {"adHoc": True, "crossVersion": None,
                                "feed": [_row(addedN=0, removedN=0), _row("added", addedN=12, removedN=0)]})
    assert ch["note"] == mcp_mod.PAIR_UNCOUNTED


def test_the_new_apis_note_and_null_counts_pass_through(monkeypatch):
    note = "These two runs were read under different extraction rules: ..."
    withheld = [{"url": "https://x.test/b", "reason": "read under different extraction rules",
                 "addedN": None, "removedN": None, "counted": False}]
    ch = _changes(monkeypatch, {"adHoc": True, "crossVersion": [3, 4], "note": note, "readDifferentlyCount": 1,
                                "feed": [_row(added="", removed="", addedN=None, removedN=None, counted=False)],
                                "withheld": withheld})
    assert ch["note"] == note, "the API's own note wins"
    row = ch["feed"][0]
    assert row["addedN"] is None and row["removedN"] is None and row["counted"] is False, "null is never 0"
    assert ch["withheld"] == withheld and ch["readDifferentlyCount"] == 1


def test_the_new_apis_pair_read_alike_gets_no_note(monkeypatch):
    ch = _changes(monkeypatch, {"adHoc": True, "crossVersion": None, "note": "",
                                "feed": [_row(addedN=3, removedN=1, counted=True),
                                         _row(addedN=None, removedN=None, counted=False)]})
    assert ch["note"] == ""


def test_a_stored_record_is_left_alone(monkeypatch):
    ch = _changes(monkeypatch, {"adHoc": False, "feed": [_row(addedN=0, removedN=0)]})
    assert "note" not in ch


def test_get_changes_names_what_a_pair_carries():
    import asyncio
    tool = {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}["get_changes"]
    change = json.dumps(tool.output_schema)
    for word in ("adHoc", "crossVersion", "note", "counted", "readDifferentlyCount"):
        assert word in change, word
        assert word in tool.description, word


# --- screenshot_url: the cost the API reports ------------------------------

class _Shot:
    def __init__(self, envelope):
        self.envelope = envelope

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract(self, url, config=None, timeout=None):
        return self.envelope


@pytest.mark.parametrize("envelope,credits", [
    # The ledger's sum wins over the page's price worked out again.
    ({"id": "r1", "page": {"screenshot": False, "screenshotError": "no frame", "pageStatus": "ok", "credits": 4},
      "creditsUsed": 1}, 1),
    # An envelope that does not say: the page's own credits.
    ({"id": "r1", "page": {"screenshot": False, "pageStatus": "ok", "credits": 4}}, 4),
])
def test_a_missing_picture_reports_what_the_api_charged(monkeypatch, envelope, credits):
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Shot(envelope))
    out = mcp_mod.screenshot_url("https://example.com/")
    assert out["code"] == "no_screenshot" and out["credits"] == credits


def test_screenshot_url_states_no_price_for_a_missing_picture():
    import asyncio
    tool = {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}["screenshot_url"]
    assert "still costs" not in tool.description
    assert "never charged for the picture" in tool.description
