import pathlib

import httpx

import mesharc
from mesharc import Crawl, MeshArc, MeshArcError


class FakeHttp:
    """Answers like the API, and remembers what it was asked."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, path, idempotency_key=None, **kw):
        self.calls.append((method, path, kw.get("params") or {}, kw.get("json") or {}, idempotency_key))
        return self.replies.pop(0) if self.replies else {}

    def pace(self, floor=3):
        pass


def client(replies):
    arc = MeshArc.__new__(MeshArc)
    arc._h = FakeHttp(replies)
    return arc, arc._h


def test_one_url_scrapes_synchronously_and_answers_with_the_page():
    arc, http = client([{"id": "r1", "status": "done", "data": [{"url": "https://x.test/", "markdown": "hi"}]}])
    assert arc.scrape("https://x.test/")["markdown"] == "hi"
    assert http.calls[0][1] == "/scrape"
    assert http.calls[0][3]["url"] == "https://x.test/"


def test_a_list_of_urls_is_a_batch():
    arc, http = client([{"id": "b1", "status": "queued"}, {"id": "b1", "status": "done", "pages": []}])
    assert arc.scrape(["https://x.test/a"])["status"] == "done"
    assert http.calls[0][1] == "/scrape"
    assert http.calls[0][3].get("urls") == ["https://x.test/a"]


def test_a_slow_page_is_polled_not_given_up_on():
    arc, http = client([{"id": "r2", "status": "running"},
                        {"id": "r2", "status": "done", "data": [{"url": "https://x.test/", "markdown": "late"}]}])
    assert arc.scrape_one("https://x.test/", poll=0)["markdown"] == "late"
    assert [c[1] for c in http.calls] == ["/scrape", "/scrape/r2"]


def test_a_crawl_carries_its_idempotency_key_and_the_bodys_own_names():
    arc, http = client([{"id": "c1", "status": "queued"}])
    arc.crawl("https://x.test/", limit=5, idempotency_key="k-9")
    assert http.calls[0][1] == "/crawl"
    assert http.calls[0][4] == "k-9"
    assert http.calls[0][3] == {"url": "https://x.test/", "limit": 5}


def test_the_page_walk_follows_the_tail_cursor_and_never_repeats_a_page():
    pages = [
        {"status": "running", "data": [{"url": "a"}], "next": None, "cursor": "c1"},
        {"status": "running", "data": [], "next": None, "cursor": "c1"},
        {"status": "running", "data": [{"url": "b"}, {"url": "c"}], "next": "/api/v1/crawl/x?cursor=c3", "cursor": "c2"},
        {"status": "done", "data": [{"url": "d"}], "next": None, "cursor": "c4"},
    ]
    http = FakeHttp(pages)
    job = Crawl(http, {"id": "x", "url": "https://x.test/", "status": "queued"})
    assert [row["url"] for row in job.pages(limit=2, poll=0)] == ["a", "b", "c", "d"]
    assert [c[2].get("cursor") for c in http.calls] == [None, "c1", "c1", "c3"]


def test_a_next_link_hands_over_its_cursor():
    assert mesharc._cursor_of("/api/v1/crawl/x?limit=5&cursor=abc%3D") == "abc="
    assert mesharc._cursor_of("/api/v1/crawl/x") == ""


def test_one_page_of_a_crawl_is_asked_for_by_url():
    arc, http = client([{"id": "c1", "status": "done"},
                        {"url": "https://x.test/a", "markdown": "all of it"}])
    crawl = arc.crawl("https://x.test/")
    assert crawl.page("https://x.test/a")["markdown"] == "all of it"
    assert http.calls[-1][:2] == ("GET", "/crawl/c1/page")
    assert http.calls[-1][2] == {"url": "https://x.test/a"}


def test_the_page_walk_can_start_from_a_cursor():
    """A caller that read the first window asks for the rest from where it
    stopped, rather than from the top."""
    arc, http = client([{"id": "c1", "status": "done"},
                        {"status": "done", "data": [{"url": "z"}], "cursor": "c9"}])
    crawl = arc.crawl("https://x.test/")
    assert [p["url"] for p in crawl.pages(cursor="c7")] == ["z"]
    assert http.calls[-1][2]["cursor"] == "c7"


def test_an_error_carries_its_code_and_request_id():
    err = MeshArcError(429, "too many", "rate_limited", "req_abc")
    assert (err.status, err.code, err.request_id) == (429, "rate_limited", "req_abc")
    assert "req_abc" in str(err)


def test_the_rate_limit_window_is_read_from_every_response(monkeypatch):
    arc = MeshArc("mesharc_test")
    arc._h._note_limits(httpx.Response(200, headers={"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "30"}))
    assert arc._h._remaining == 2
    arc._h._note_limits(httpx.Response(200, headers={"X-RateLimit-Remaining": "not a number"}))
    assert arc._h._remaining == 2
    arc._h._note_limits(httpx.Response(200))
    assert arc._h._remaining == 2


def test_pace_waits_out_the_window_only_when_nearly_out(monkeypatch):
    slept = []
    monkeypatch.setattr(mesharc.time, "sleep", lambda s: slept.append(s))
    arc = MeshArc("mesharc_test")
    h = arc._h
    h._remaining, h._reset_at = 10, mesharc.time.monotonic() + 30
    h.pace()
    assert slept == []
    h._remaining = 2
    h.pace()
    assert len(slept) == 1 and 0 < slept[0] <= 30
    assert h._remaining is None
    h._remaining, h._reset_at = 0, mesharc.time.monotonic() + 600
    h.pace()
    assert slept[-1] == 60.0


def test_the_mcp_server_offers_the_verbs_and_the_config_tools():
    source = (pathlib.Path(mesharc.__file__).parent / "mcp.py").read_text(encoding="utf-8")
    for name in ("scrape_urls", "extract_url", "map_site", "crawl_site", "keep_crawl_as_project",
                 "list_projects", "get_changes", "search_pages", "recrawl_pages", "get_job", "cancel_job", "list_runs",
                 "describe_project_config", "get_project", "create_project", "update_project"):
        assert f"def {name}(" in source, name
