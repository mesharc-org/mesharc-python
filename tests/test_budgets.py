"""The hosted MCP never holds a call past its wait: a one-URL scrape, and a
full=true read, come back as a job for get_job when they outlast it (MCP
re-test 7, 12 Nov 2026: a one-URL scrape of a Cloudflare page ran 230 s with
nothing back, until the caller gave up)."""

import pytest

from mesharc import MeshArcTimeoutError
from mesharc import mcp as mcp_mod


class _Slow:
    """A client whose page is still being read: scrape_one and extract run
    out of time, as a walled page does on the server."""

    def __init__(self, envelope=None, extract=None):
        self.asked, self.cancelled = [], []
        self._envelope, self._extract = envelope, extract

    def scrape_one(self, url, **kw):
        self.asked.append(("scrape_one", kw.get("timeout_s"), kw.get("timeout")))
        raise MeshArcTimeoutError("scrape j1 is still running", "j1")

    def scrape(self, url, **kw):
        self.asked.append(("scrape", kw.get("timeout")))
        return {"url": url, "status": "ok", "words": 5, "markdown": "m"}

    def extract(self, url, **kw):
        self.asked.append(("extract", kw.get("timeout")))
        raise MeshArcTimeoutError("extraction x1 is still running", "x1")

    def batch(self, job_id, **_kw):
        return self._envelope

    def get_extract(self, job_id):
        return self._extract

    def cancel_batch(self, job_id):
        self.cancelled.append(job_id)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_budget", lambda: 7.0)


def test_one_url_past_the_wait_is_a_job_and_the_wait_is_the_budget(monkeypatch, hosted):
    slow = _Slow()
    monkeypatch.setattr(mcp_mod, "_client", lambda: slow)
    out = mcp_mod.scrape_urls(["https://walled.test/"])
    assert out["status"] == "running" and out["job"] == {"kind": "batch", "id": "j1"}
    assert slow.asked == [("scrape_one", 7, 0)], "the API holds the request for the wait, and nothing polls after it"


def test_one_url_comes_back_within_the_wait_against_an_api_that_holds_it(monkeypatch, hosted):
    """Timed through the real client: the API holds the POST for the whole
    wait and answers it unfinished. Before, the client then polled for the wait
    again -- twice the wait, ~90 s hosted."""
    import time

    from mesharc import MeshArc

    class _Holding:
        def __call__(self, method, path, json=None, params=None, idempotency_key=None, **kw):
            if method == "POST":
                time.sleep(json.get("timeout", 0))
            return {"id": "s1", "status": "running"}

        def pace(self):
            pass

    class _Ctx:
        def __enter__(self):
            c = MeshArc.__new__(MeshArc)
            c._h = _Holding()
            return c

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mcp_mod, "_budget", lambda: 1.0)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Ctx())
    t = time.time()
    out = mcp_mod.scrape_urls(["https://walled.test/"])
    took = time.time() - t
    assert out["job"] == {"kind": "batch", "id": "s1"}
    assert took < 1.6, f"{took:.1f}s for a 1 s wait"


def test_one_url_in_stdio_waits_as_it_always_did(monkeypatch):
    slow = _Slow()
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    monkeypatch.setattr(mcp_mod, "_client", lambda: slow)
    out = mcp_mod.scrape_urls(["https://ok.test/"])
    assert out["status"] == "done" and out["pages"][0]["url"] == "https://ok.test/"
    assert slow.asked == [("scrape", None)]


def test_a_full_read_past_the_wait_is_an_extract_job(monkeypatch, hosted):
    slow = _Slow()
    monkeypatch.setattr(mcp_mod, "_client", lambda: slow)
    out = mcp_mod.scrape_urls(["https://walled.test/"], full=True)
    assert out["job"] == {"kind": "extract", "id": "x1"} and slow.asked == [("extract", 7.0)]


def test_get_job_reads_a_one_url_scrape_as_one_page(monkeypatch, hosted):
    page = {"url": "https://ok.test/", "status": "ok", "words": 5, "markdown": "m", "links": [{"href": "x"}]}
    done = {"id": "j1", "kind": "scrape", "status": "done", "data": [page]}
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Slow(envelope=done))
    out = mcp_mod.get_job("batch", "j1")
    assert out["status"] == "done" and out["urls"] == 1 and out["pages"][0]["url"] == "https://ok.test/"
    running = {"id": "j1", "kind": "scrape", "status": "running", "data": []}
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Slow(envelope=running))
    assert mcp_mod.get_job("batch", "j1")["job"] == {"kind": "batch", "id": "j1"}
    failed = {"id": "j1", "kind": "scrape", "status": "error", "data": [], "error": "the worker died"}
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Slow(envelope=failed))
    out = mcp_mod.get_job("batch", "j1")
    assert out["status"] == 502 and "the worker died" in out["error"]


def test_get_job_reads_an_extract_and_cancel_job_stops_one(monkeypatch, hosted):
    done = {"id": "x1", "status": "done", "page": {"url": "https://ok.test/", "links": [1, 2]},
            "config": {"render_js": "auto", "formats": ["markdown"]}}
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Slow(extract=done))
    out = mcp_mod.get_job("extract", "x1")
    assert out["page"]["url"] == "https://ok.test/" and "links" not in out["page"] and out["config"] == {}
    running = _Slow(extract={"id": "x1", "status": "running"})
    monkeypatch.setattr(mcp_mod, "_client", lambda: running)
    assert mcp_mod.get_job("extract", "x1")["job"] == {"kind": "extract", "id": "x1"}
    assert mcp_mod.cancel_job("extract", "x1")["outcome"] == "cancelling" and running.cancelled == ["x1"]
    finished = _Slow(extract={"id": "x1", "status": "done"})
    monkeypatch.setattr(mcp_mod, "_client", lambda: finished)
    assert mcp_mod.cancel_job("extract", "x1")["outcome"] == "done" and finished.cancelled == []
