import pathlib

import httpx
import pytest

import mesharc
from mesharc import Crawl, MeshArc, MeshArcError, MeshArcTimeoutError


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


def test_a_search_answered_at_once_returns_the_whole_envelope_and_sends_camelcase():
    done = {"id": "s1", "status": "done", "data": [{"url": "https://x.test/", "title": "X"}]}
    arc, http = client([done])
    assert arc.web_search("mesh arc", limit=5, freshness="week", include_domains=["x.test"],
                          exclude_domains=("y.test",), scrape=True, idempotency_key="k-s") == done
    assert http.calls == [("POST", "/search", {}, {
        "query": "mesh arc", "limit": 5, "freshness": "week", "includeDomains": ["x.test"],
        "excludeDomains": ["y.test"], "scrape": {"formats": ["markdown"]}, "timeout": 60}, "k-s")]


def test_a_queued_search_is_polled_until_done():
    arc, http = client([{"id": "s2", "status": "queued"}, {"id": "s2", "status": "running"},
                        {"id": "s2", "status": "done", "data": [{"url": "a"}]}])
    assert arc.web_search("q", poll=0)["data"] == [{"url": "a"}]
    assert [c[:3] for c in http.calls[1:]] == [("GET", "/search/s2", {}), ("GET", "/search/s2", {})]


def test_a_blocked_search_is_returned_not_raised():
    arc, http = client([{"id": "s3", "status": "running"}, {"id": "s3", "status": "blocked", "data": []}])
    assert arc.web_search("q", poll=0)["status"] == "blocked"
    assert len(http.calls) == 2


def test_a_search_that_ends_in_error_raises_with_the_apis_text():
    arc, _ = client([{"id": "s4", "status": "error", "error": "every engine failed"}])
    with pytest.raises(MeshArcError) as exc:
        arc.web_search("q", poll=0)
    assert (exc.value.status, exc.value.code, exc.value.detail) == (502, "job_failed", "every engine failed")


def test_a_search_still_running_at_the_deadline_raises_a_timeout_naming_it():
    arc, _ = client([{"id": "s5", "status": "running"}])
    with pytest.raises(MeshArcTimeoutError) as exc:
        arc.web_search("q", poll=0, timeout=0)
    assert exc.value.job_id == "s5"


def test_a_search_without_waiting_asks_once_with_no_hold():
    arc, http = client([{"id": "s6", "status": "queued"}])
    assert arc.web_search("q", wait=False) == {"id": "s6", "status": "queued"}
    assert len(http.calls) == 1
    assert http.calls[0][3] == {"query": "q", "timeout": 0}


def test_a_search_sends_an_explicit_hold_and_a_scrape_dict_as_given():
    scrape = {"formats": ["markdown", "links"], "maxCredits": 20}
    arc, http = client([{"id": "s7", "status": "queued"}, {"id": "s8", "status": "done"}])
    arc.web_search("q", wait=False, timeout_s=30, scrape=scrape)
    assert http.calls[0][3] == {"query": "q", "scrape": scrape, "timeout": 30}
    arc.web_search("q", scrape=False)
    assert "scrape" not in http.calls[1][3]
    assert http.calls[1][3]["timeout"] == 60


def test_a_search_started_earlier_is_asked_for_by_id():
    arc, http = client([{"id": "s9", "status": "done"}])
    assert arc.get_search("s9") == {"id": "s9", "status": "done"}
    assert http.calls == [("GET", "/search/s9", {}, {}, None)]


def test_the_search_list_follows_next_and_sends_q_and_limit_on_every_page():
    arc, http = client([{"data": [{"id": "a"}, {"id": "b"}], "next": "/api/v1/search?limit=2&q=mesh&cursor=c2"},
                        {"data": [{"id": "c"}], "next": None}])
    assert [row["id"] for row in arc.searches(q="mesh", limit=2)] == ["a", "b", "c"]
    assert [c[:3] for c in http.calls] == [("GET", "/search", {"limit": 2, "q": "mesh"}),
                                           ("GET", "/search", {"limit": 2, "q": "mesh", "cursor": "c2"})]


def test_the_project_search_still_asks_the_projects_pages():
    arc, http = client([{"data": []}])
    arc.search("p1", "pricing")
    assert http.calls[0][:2] == ("POST", "/projects/p1/pages/search")
    assert http.calls[0][3] == {"mode": "content", "q": "pricing", "run_id": None}


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
    for name in ("scrape_urls", "map_site", "crawl_site", "web_search", "list_projects", "get_changes",
                 "get_job", "cancel_job", "list_runs", "list_pages", "get_page", "start_run", "get_project",
                 "create_project", "update_project", "delete_project"):
        assert f"def {name}(" in source, name
