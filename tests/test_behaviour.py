import pathlib
from datetime import date, datetime, timezone

import httpx
import pytest

import mesharc
from mesharc import AgentRun, Crawl, MeshArc, MeshArcError, MeshArcTimeoutError


class FakeHttp:
    """Answers like the API, and remembers what it was asked."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, path, idempotency_key=None, **kw):
        self.calls.append((method, path, kw.get("params") or {}, kw.get("json") or {}, idempotency_key))
        return self.replies.pop(0) if self.replies else {}

    def pace(self, floor=3):
        pass


def client(replies, http_class=None):
    arc = MeshArc.__new__(MeshArc)
    arc._h = (http_class or FakeHttp)(replies)
    arc.monitors = mesharc._Monitors(arc._h)
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
    for name in ("scrape_urls", "map_site", "crawl_site", "search_web", "list_projects", "get_changes",
                 "get_job", "cancel_job", "list_runs", "list_pages", "start_run", "get_project",
                 "create_project", "update_project", "delete_project", "run_agent", "continue_agent"):
        assert f"def {name}(" in source, name


class RaisingHttp(FakeHttp):
    """A FakeHttp whose replies may be errors, raised when their turn comes."""

    def __call__(self, method, path, idempotency_key=None, **kw):
        reply = super().__call__(method, path, idempotency_key, **kw)
        if isinstance(reply, Exception):
            raise reply
        return reply


def expired():
    return MeshArcError(410, "this run has expired", "expired")


def test_an_agent_run_sends_camelcase_and_caps_the_hold():
    arc, http = client([{"id": "a1", "status": "queued"}])
    run = arc.agent("who makes it?", urls=("https://a.test/",), schema={"type": "object"}, max_credits=50,
                    max_steps=5, allowed_domains=["a.test"], timeout_s=300, idempotency_key="k-a")
    assert isinstance(run, AgentRun)
    assert (run.id, run.status) == ("a1", "queued")
    assert http.calls == [("POST", "/agent", {}, {
        "prompt": "who makes it?", "urls": ["https://a.test/"], "schema": {"type": "object"}, "maxCredits": 50,
        "maxSteps": 5, "allowedDomains": ["a.test"], "timeout": 120}, "k-a")]


def test_an_agent_run_leaves_out_what_was_not_given_and_sends_no_hold_by_default():
    arc, http = client([{"id": "a2", "status": "queued"}, {"id": "a3", "status": "done"}])
    arc.agent("q")
    assert http.calls[0][3] == {"prompt": "q"}
    assert http.calls[0][4] is None
    run = arc.agent("q", timeout_s=30)
    assert http.calls[1][3] == {"prompt": "q", "timeout": 30}
    assert run.status == "done"


def test_an_agent_run_is_polled_until_done():
    done = {"id": "a1", "status": "done", "data": {"text": "MeshArc"}, "sources": [{"url": "https://a.test/"}]}
    arc, http = client([{"id": "a1", "status": "queued"}, {"id": "a1", "status": "running"}, done])
    run = arc.agent("q")
    assert run.wait(poll=0) == done
    assert [c[:2] for c in http.calls[1:]] == [("GET", "/agent/a1"), ("GET", "/agent/a1")]
    assert run.data == {"text": "MeshArc"}
    assert run.sources == [{"url": "https://a.test/"}]


@pytest.mark.parametrize("status", ["credit_limit", "cancelled"])
def test_an_agent_run_that_stopped_short_is_returned_not_raised(status):
    end = {"id": "a1", "status": status, "data": {"partial": "so far"}}
    http = FakeHttp([{"id": "a1", "status": "running"}, end])
    assert AgentRun(http, {"id": "a1", "status": "queued"}).wait(poll=0) == end
    assert len(http.calls) == 2


def test_an_agent_run_that_ends_in_error_raises_with_the_apis_text():
    http = FakeHttp([{"id": "a1", "status": "error", "error": "the model refused"}])
    with pytest.raises(MeshArcError) as exc:
        AgentRun(http, {"id": "a1", "status": "running"}).wait(poll=0)
    assert (exc.value.status, exc.value.code, exc.value.detail) == (502, "job_failed", "the model refused")


def test_an_agent_run_still_running_at_the_deadline_raises_a_timeout_naming_it():
    http = FakeHttp([{"id": "a9", "status": "running"}])
    with pytest.raises(MeshArcTimeoutError) as exc:
        AgentRun(http, {"id": "a9", "status": "queued"}).wait(poll=0, timeout=0)
    assert exc.value.job_id == "a9"
    assert "agent a9 is still running" in str(exc.value)


def test_cancelling_an_agent_run_returns_the_apis_answer_and_leaves_the_envelope():
    http = FakeHttp([{"id": "a1", "status": "cancelling"}])
    run = AgentRun(http, {"id": "a1", "status": "running"})
    assert run.cancel() == {"id": "a1", "status": "cancelling"}
    assert http.calls == [("DELETE", "/agent/a1", {}, {}, None)]
    assert run.status == "running"


def test_an_agent_run_is_asked_for_by_id():
    arc, http = client([{"id": "a1", "status": "done"}])
    run = arc.get_agent("a1")
    assert (run.id, run.status) == ("a1", "done")
    assert http.calls == [("GET", "/agent/a1", {}, {}, None)]


def test_an_expired_agent_run_raises_410_unchanged():
    arc = MeshArc.__new__(MeshArc)
    arc._h = RaisingHttp([expired()])
    with pytest.raises(MeshArcError) as exc:
        arc.get_agent("a1")
    assert (exc.value.status, exc.value.code) == (410, "expired")


def test_the_agent_list_follows_next_and_sends_status_and_limit_on_every_page():
    arc, http = client([{"data": [{"id": "a"}, {"id": "b"}], "next": "/api/v1/agent?limit=2&status=done&cursor=17"},
                        {"data": [{"id": "c"}], "next": None}])
    assert [row["id"] for row in arc.agent_runs(status="done", limit=2)] == ["a", "b", "c"]
    assert [c[:3] for c in http.calls] == [("GET", "/agent", {"limit": 2, "status": "done"}),
                                           ("GET", "/agent", {"limit": 2, "status": "done", "cursor": "17"})]


def test_the_trace_refreshes_before_each_drain_and_ends_after_the_drain_that_follows_the_end():
    http = FakeHttp([{"id": "a1", "status": "running"},
                     {"data": [{"seq": 1, "kind": "start"}, {"seq": 2, "kind": "search"}], "last": 2},
                     {"id": "a1", "status": "done"},
                     {"data": [{"seq": 3, "kind": "finish"}], "last": 3},
                     {"unexpected": True}])
    run = AgentRun(http, {"id": "a1", "status": "queued"})
    assert [e["seq"] for e in run.trace(poll=0)] == [1, 2, 3]
    assert [c[:3] for c in http.calls] == [("GET", "/agent/a1", {}),
                                           ("GET", "/agent/a1/trace", {"after": 0, "limit": 500}),
                                           ("GET", "/agent/a1", {}),
                                           ("GET", "/agent/a1/trace", {"after": 2, "limit": 500})]
    assert run.status == "done"


def test_the_trace_without_follow_drains_once_reading_on_while_pages_come_back_full():
    full = [{"seq": n} for n in range(1, 501)]
    http = FakeHttp([{"data": full, "last": 500}, {"data": [{"seq": 501}], "last": 501}, {"unexpected": True}])
    run = AgentRun(http, {"id": "a1", "status": "running"})
    assert len(list(run.trace(after=0, follow=False))) == 501
    assert [c[:3] for c in http.calls] == [("GET", "/agent/a1/trace", {"after": 0, "limit": 500}),
                                           ("GET", "/agent/a1/trace", {"after": 500, "limit": 500})]


def test_the_trace_resumes_after_the_seq_it_is_given():
    http = FakeHttp([{"data": [], "last": 7}])
    assert list(AgentRun(http, {"id": "a1", "status": "done"}).trace(after=7, follow=False)) == []
    assert http.calls[0][2] == {"after": 7, "limit": 500}


def test_the_trace_of_an_expired_run_ends_quietly():
    http = RaisingHttp([{"id": "a1", "status": "running"}, {"data": [{"seq": 1}], "last": 1}, expired()])
    assert [e["seq"] for e in AgentRun(http, {"id": "a1", "status": "running"}).trace(poll=0)] == [1]
    http = RaisingHttp([{"id": "a1", "status": "running"}, expired()])
    assert list(AgentRun(http, {"id": "a1", "status": "running"}).trace(poll=0)) == []


def test_the_trace_still_running_at_the_deadline_raises_a_timeout_naming_it():
    http = FakeHttp([{"id": "a4", "status": "running"}, {"data": [], "last": 0}])
    with pytest.raises(MeshArcTimeoutError) as exc:
        list(AgentRun(http, {"id": "a4", "status": "queued"}).trace(poll=0, timeout=0))
    assert exc.value.job_id == "a4"


def test_an_agent_run_sends_its_webhook_and_connection_and_keeps_the_secret_past_a_refresh():
    hook = {"url": "https://hooks.test/agent", "events": ["agent.completed"], "metadata": {"team": "a"}}
    arc, http = client([{"id": "a5", "status": "queued", "webhookSecret": "whsec_1"}, {"id": "a5", "status": "running"}])
    run = arc.agent("q", webhook=hook, connection_id="conn_9")
    assert http.calls[0][3] == {"prompt": "q", "webhook": hook, "connectionId": "conn_9"}
    assert run.webhook_secret == "whsec_1"
    run.refresh()
    assert "webhookSecret" not in run.envelope
    assert run.webhook_secret == "whsec_1"


def test_an_agent_runs_webhook_may_be_a_bare_url():
    arc, http = client([{"id": "a6", "status": "queued", "webhookSecret": "whsec_2"}])
    assert arc.agent("q", webhook="https://hooks.test/a").webhook_secret == "whsec_2"
    assert http.calls[0][3] == {"prompt": "q", "webhook": "https://hooks.test/a"}


def test_an_agent_run_without_a_webhook_or_links_answers_empty_not_missing():
    run = AgentRun(FakeHttp([]), {"id": "a7", "status": "done", "fieldSources": None, "continuesRunId": None})
    assert run.webhook_secret == ""
    assert run.field_sources == {}
    assert (run.continues_run_id, run.continued_by) == (None, None)
    assert AgentRun(FakeHttp([]), {"id": "a8"}).field_sources == {}


def test_an_agent_runs_field_sources_and_thread_links_are_read_from_the_envelope():
    sources = {"plans[0].price": {"url": "https://a.test/pricing", "pageId": "p1"},
               "[2].name": {"url": "https://a.test/", "pageId": "p2"}}
    run = AgentRun(FakeHttp([]), {"id": "a9", "status": "credit_limit", "fieldSources": sources,
                                  "threadId": "a1", "continuesRunId": "a1", "continuedBy": "a10"})
    assert run.field_sources == sources
    assert (run.continues_run_id, run.continued_by) == ("a1", "a10")


def test_continuing_a_run_posts_its_new_budget_and_hands_back_the_new_run():
    http = FakeHttp([{"id": "a2", "status": "running", "continuesRunId": "a1", "threadId": "a1"}])
    stopped = AgentRun(http, {"id": "a1", "status": "credit_limit", "data": {"partial": "so far"}})
    run = stopped.continue_(max_credits=800, max_steps=10, timeout_s=300, idempotency_key="k-c")
    assert isinstance(run, AgentRun) and run is not stopped
    assert (run.id, run.status, run.continues_run_id) == ("a2", "running", "a1")
    assert http.calls == [("POST", "/agent/a1/continue", {}, {"maxCredits": 800, "maxSteps": 10, "timeout": 120},
                           "k-c")]
    assert stopped.status == "credit_limit"


def test_continuing_a_run_keeps_the_stopped_runs_webhook_secret():
    """The API carries the webhook and its secret over to the new run, but the
    continue answer does not repeat the secret: the new handle keeps the old one's."""
    arc, http = client([{"id": "a1", "status": "queued", "webhookSecret": "whsec_1"},
                        {"id": "a1", "status": "credit_limit"},
                        {"id": "a2", "status": "running", "continuesRunId": "a1"},
                        {"id": "a2", "status": "running"}])
    stopped = arc.agent("q", webhook="https://hooks.test/a")
    stopped.refresh()
    assert "webhookSecret" not in stopped.envelope
    more = stopped.continue_()
    assert more.webhook_secret == "whsec_1"
    more.refresh()
    assert more.webhook_secret == "whsec_1", "kept past a refresh, as the first handle's is"


def test_continuing_a_run_without_a_webhook_or_from_a_reopened_handle_has_no_secret():
    http = FakeHttp([{"id": "a2", "status": "queued"}])
    assert AgentRun(http, {"id": "a1", "status": "credit_limit"}).continue_().webhook_secret == ""


def test_continuing_a_run_leaves_out_what_was_not_given():
    http = FakeHttp([{"id": "a2", "status": "queued"}])
    AgentRun(http, {"id": "a1", "status": "credit_limit"}).continue_()
    assert http.calls == [("POST", "/agent/a1/continue", {}, {}, None)]


@pytest.mark.parametrize("error", [
    MeshArcError(409, "only a run stopped at its credit limit can be continued; this one is done", "conflict"),
    MeshArcError(409, "this run was already continued, by run a2", "conflict"),
    MeshArcError(404, "agent run not found", "not_found"),
    MeshArcError(410, "this run is past its keep date; its saved progress is gone", "expired"),
])
def test_continuing_a_run_the_api_refuses_raises_its_error_unchanged(error):
    http = RaisingHttp([error])
    with pytest.raises(MeshArcError) as exc:
        AgentRun(http, {"id": "a1", "status": "credit_limit"}).continue_()
    assert exc.value is error
    assert len(http.calls) == 1


def test_the_agent_list_sends_model_since_and_until_on_every_page():
    arc, http = client([{"data": [{"id": "a"}], "next": "/api/v1/agent?limit=1&cursor=1&model=openai%3Agpt-5.4-mini"},
                        {"data": [{"id": "b"}], "next": None}])
    rows = arc.agent_runs(model="openai:gpt-5.4-mini", since="2026-10-01", until="2026-10-08T12:00:00Z", limit=1)
    assert [row["id"] for row in rows] == ["a", "b"]
    wanted = {"limit": 1, "model": "openai:gpt-5.4-mini", "since": "2026-10-01", "until": "2026-10-08T12:00:00Z"}
    assert [c[:3] for c in http.calls] == [("GET", "/agent", wanted), ("GET", "/agent", {**wanted, "cursor": "1"})]


def test_the_agent_list_sends_a_date_or_datetime_as_iso_8601():
    arc, http = client([{"data": [], "next": None}])
    list(arc.agent_runs(status="done", since=date(2026, 10, 1),
                        until=datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)))
    assert http.calls[0][2] == {"limit": 25, "status": "done", "since": "2026-10-01",
                                "until": "2026-10-08T09:30:00+00:00"}


def test_the_agent_list_sends_no_filter_it_was_not_given():
    arc, http = client([{"data": [], "next": None}])
    assert list(arc.agent_runs()) == []
    assert http.calls[0][2] == {"limit": 25}


def test_the_agent_list_sends_no_since_or_until_for_an_empty_string():
    arc, http = client([{"data": [], "next": None}])
    assert list(arc.agent_runs(since="", until="")) == []
    assert http.calls[0][2] == {"limit": 25}


def test_a_naive_datetime_is_sent_without_a_zone_for_the_api_to_read_as_utc():
    assert mesharc._iso(datetime(2026, 10, 8, 9, 30)) == "2026-10-08T09:30:00"
    assert mesharc._iso(date(2026, 10, 8)) == "2026-10-08"
    assert mesharc._iso("2026-10-08T09:30:00+02:00") == "2026-10-08T09:30:00+02:00"


def test_a_news_search_sends_sources_news_and_the_results_page():
    hit = {"url": "https://n.test/a", "title": "A", "publisher": "N", "age": "20h"}
    arc, http = client([{"id": "s10", "status": "done", "data": [hit]}])
    out = arc.web_search("mesh arc", news=True, page=2)
    assert (out["data"][0]["publisher"], out["data"][0]["age"]) == ("N", "20h")
    assert http.calls[0][3] == {"query": "mesh arc", "sources": ["news"], "page": 2, "timeout": 60}


def test_a_web_search_sends_no_sources_or_page_unless_asked():
    arc, http = client([{"id": "s11", "status": "done", "data": []}])
    arc.web_search("q", news=False)
    assert http.calls[0][3] == {"query": "q", "timeout": 60}


def test_a_monitor_is_created_with_the_apis_names_and_an_idempotency_key():
    monitor = {"id": "m1", "kind": "search", "status": "active", "webhookSecret": "whsec_m"}
    arc, http = client([monitor])
    request = {"query": "mesh arc", "freshness": "day"}
    hook = {"url": "https://hooks.test/m", "events": ["search.changed"]}
    assert arc.monitors.create("search", request, "daily", name="Mentions", webhook=hook, baseline_id="s1",
                               idempotency_key="k-m") == monitor
    assert http.calls == [("POST", "/monitors", {}, {"kind": "search", "request": request, "schedule": "daily",
                                                     "name": "Mentions", "webhook": hook, "baselineId": "s1"}, "k-m")]


def test_a_monitor_leaves_out_what_was_not_given():
    arc, http = client([{"id": "m2", "kind": "agent"}])
    arc.monitors.create("agent", {"prompt": "q"}, "weekly")
    assert http.calls == [("POST", "/monitors", {}, {"kind": "agent", "request": {"prompt": "q"},
                                                     "schedule": "weekly"}, None)]


def test_the_monitor_list_unwraps_data_and_filters_by_kind():
    arc, http = client([{"data": [{"id": "m1"}, {"id": "m2"}]}, {"data": []}])
    assert [m["id"] for m in arc.monitors.list()] == ["m1", "m2"]
    assert arc.monitors.list(kind="agent") == []
    assert [c[:3] for c in http.calls] == [("GET", "/monitors", {}), ("GET", "/monitors", {"kind": "agent"})]


def test_a_monitor_is_read_paused_resumed_and_deleted_by_id():
    arc, http = client([{"id": "m1", "status": "active"}, {"id": "m1", "status": "paused"},
                        {"id": "m1", "status": "active"}, {"id": "m1", "deleted": True}])
    assert arc.monitors.get("m1")["status"] == "active"
    assert arc.monitors.pause("m1")["status"] == "paused"
    assert arc.monitors.resume("m1")["status"] == "active"
    assert arc.monitors.delete("m1") == {"id": "m1", "deleted": True}
    assert [c[:2] for c in http.calls] == [("GET", "/monitors/m1"), ("POST", "/monitors/m1/pause"),
                                           ("POST", "/monitors/m1/resume"), ("DELETE", "/monitors/m1")]


def test_updating_a_monitor_sends_only_the_given_fields_and_an_empty_webhook_to_remove_it():
    arc, http = client([{"id": "m1"}, {"id": "m1", "webhook": None}])
    arc.monitors.update("m1", schedule="hourly", status="paused")
    arc.monitors.update("m1", webhook="")
    assert [c[:4] for c in http.calls] == [("PATCH", "/monitors/m1", {}, {"schedule": "hourly", "status": "paused"}),
                                           ("PATCH", "/monitors/m1", {}, {"webhook": ""})]


def test_updating_a_monitor_sends_nothing_for_a_none_and_keeps_an_empty_webhook():
    """None means "leave it": a caller passing a variable that is None must not
    clear the field. Only the empty string removes the webhook."""
    arc, http = client([{"id": "m1"}, {"id": "m1"}])
    arc.monitors.update("m1", name=None, schedule=None, status=None, webhook=None)
    arc.monitors.update("m1", name="Mentions", schedule=None, status=None, webhook="")
    assert [c[:4] for c in http.calls] == [("PATCH", "/monitors/m1", {}, {}),
                                           ("PATCH", "/monitors/m1", {}, {"name": "Mentions", "webhook": ""})]


def test_updating_a_monitor_takes_only_its_four_fields():
    arc, http = client([{"id": "m1"}])
    with pytest.raises(TypeError):
        arc.monitors.update("m1", schedul="hourly")
    assert http.calls == [], "a misspelt field is refused here, not sent"


def test_running_a_monitor_now_answers_the_new_run():
    run = {"id": "r1", "monitorId": "m1", "trigger": "manual", "status": "running", "refId": "s9"}
    arc, http = client([run])
    assert arc.monitors.run("m1", idempotency_key="k-r") == run
    assert http.calls == [("POST", "/monitors/m1/run", {}, {}, "k-r")]


def test_running_a_monitor_while_a_run_is_under_way_raises_the_apis_409():
    arc, _ = client([MeshArcError(409, "a run of this monitor is under way", "conflict")], RaisingHttp)
    with pytest.raises(MeshArcError) as exc:
        arc.monitors.run("m1")
    assert (exc.value.status, exc.value.code) == (409, "conflict")


@pytest.mark.parametrize("call", [
    lambda arc: arc.monitors.get("nope"),
    lambda arc: arc.monitors.update("nope", name="x"),
    lambda arc: arc.monitors.pause("nope"),
    lambda arc: arc.monitors.resume("nope"),
    lambda arc: arc.monitors.run("nope"),
    lambda arc: arc.monitors.delete("nope"),
    lambda arc: list(arc.monitors.runs("nope")),
])
def test_a_missing_monitor_raises_404_unchanged(call):
    arc, _ = client([MeshArcError(404, "monitor not found", "not_found")], RaisingHttp)
    with pytest.raises(MeshArcError) as exc:
        call(arc)
    assert (exc.value.status, exc.value.code) == (404, "not_found")


def test_the_monitor_run_list_follows_next_and_sends_limit_on_every_page():
    arc, http = client([{"data": [{"id": "r1", "changed": True}, {"id": "r2"}],
                         "next": "/api/v1/monitors/m1/runs?limit=2&cursor=2"},
                        {"data": [{"id": "r3"}], "next": None}])
    assert [row["id"] for row in arc.monitors.runs("m1", limit=2)] == ["r1", "r2", "r3"]
    assert [c[:3] for c in http.calls] == [("GET", "/monitors/m1/runs", {"limit": 2}),
                                           ("GET", "/monitors/m1/runs", {"limit": 2, "cursor": "2"})]


def test_the_monitors_namespace_is_set_up_by_the_client_and_leaves_the_job_queue_alone():
    arc = MeshArc("mesharc_test")
    assert isinstance(arc.monitors, mesharc._Monitors)
    assert arc.monitors._h is arc._h, "one transport for the whole client"
    old = ("create_monitor", "get_monitor", "update_monitor", "pause_monitor", "resume_monitor", "run_monitor",
           "monitor_runs", "delete_monitor")
    assert not any(hasattr(arc, name) for name in old)
    arc.close()
    arc, http = client([{"queued": 0, "running": 1}])
    assert arc.monitor() == {"queued": 0, "running": 1}
    assert http.calls == [("GET", "/me/monitor", {}, {}, None)]
