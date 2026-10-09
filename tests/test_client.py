"""The client against a scripted transport: no network, no key."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from mesharc import AgentRun, Crawl, MeshArc, MeshArcError, __version__


def scripted(*responses):
    """A MeshArc whose transport answers from a list; the requests it saw are on `.calls`."""
    calls = []
    queue = list(responses)

    def handle(request):
        calls.append(request)
        if not queue:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        headers = {}
        if isinstance(body, dict) and "__headers__" in body:
            headers = body.pop("__headers__")
        return httpx.Response(status, json=body if status != 204 else None, headers=headers)

    arc = MeshArc("mesharc_test", max_retries=2)
    arc._h._c = httpx.Client(base_url="https://api.mesharc.dev/api/v1",
                             headers={"Authorization": "Bearer mesharc_test", "User-Agent": f"mesharc-python/{__version__}"},
                             transport=httpx.MockTransport(handle))
    arc._h._backoff = lambda attempt: 0  # type: ignore[method-assign]
    arc.calls = calls
    return arc


def test_a_key_is_required(monkeypatch):
    monkeypatch.delenv("MESHARC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        MeshArc()
    monkeypatch.setenv("MESHARC_API_KEY", "mesharc_env")
    assert MeshArc()


def test_a_call_carries_the_bearer_the_user_agent_and_an_idempotency_key():
    arc = scripted({"ok": True})
    assert arc._h("POST", "/scrape", json={"url": "https://a.test/"}, idempotency_key="once-1") == {"ok": True}
    req = arc.calls[0]
    assert str(req.url) == "https://api.mesharc.dev/api/v1/scrape"
    assert req.headers["Authorization"] == "Bearer mesharc_test"
    assert req.headers["User-Agent"] == f"mesharc-python/{__version__}"
    assert req.headers["Idempotency-Key"] == "once-1"


def test_an_error_becomes_mesharcerror_with_code_and_request_id():
    arc = scripted((402, {"error": "no credits left", "code": "plan_limit", "request_id": "req_1"}))
    with pytest.raises(MeshArcError) as exc:
        arc.me()
    assert (exc.value.status, exc.value.code, exc.value.request_id, exc.value.detail) == (402, "plan_limit", "req_1", "no credits left")


def test_a_get_is_retried_on_429_and_honours_retry_after():
    arc = scripted((429, {"error": "slow down", "code": "rate_limited", "__headers__": {"Retry-After": "0"}}), {"id": "ws"})
    assert arc.me() == {"id": "ws"}
    assert len(arc.calls) == 2


def test_a_post_without_an_idempotency_key_is_not_retried():
    arc = scripted((503, {"error": "down"}), {})
    with pytest.raises(MeshArcError) as exc:
        arc._h("POST", "/crawl", json={"url": "https://a.test/"})
    assert exc.value.status == 503
    assert len(arc.calls) == 1


def test_a_network_failure_is_retried_then_reported_as_status_0():
    arc = scripted(httpx.ConnectError("refused"), httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    with pytest.raises(MeshArcError) as exc:
        arc.me()
    assert (exc.value.status, exc.value.code) == (0, "network")
    assert len(arc.calls) == 3


def test_scrape_of_one_url_returns_the_page_polling_when_the_api_answers_with_a_job():
    arc = scripted({"id": "j1", "status": "running"},
                   {"id": "j1", "status": "done", "data": [{"url": "https://a.test/", "markdown": "# Hi", "credits": 1}]})
    page = arc.scrape("https://a.test/", config={"render_js": "auto"}, poll=0)
    assert page["markdown"] == "# Hi"
    assert json.loads(arc.calls[0].content) == {"url": "https://a.test/", "formats": "markdown", "timeout": 60, "config": {"render_js": "auto"}}
    assert arc.calls[1].method == "GET"


def test_a_job_that_outlasts_the_deadline_raises_a_timeout_that_is_both_kinds():
    arc = scripted({"id": "j2", "status": "running"}, {"id": "j2", "status": "running"})
    with pytest.raises(TimeoutError) as exc:
        arc.scrape("https://a.test/", poll=0, timeout=0)
    assert isinstance(exc.value, MeshArcError)
    assert exc.value.job_id == "j2"


def test_a_crawl_handle_pages_through_its_rows_and_follows_the_cursor():
    arc = scripted({"id": "c1", "status": "running", "url": "https://d.test/", "webhookSecret": "whsec_x"},
                   {"id": "c1", "status": "running", "data": [{"url": "https://d.test/a"}], "next": "/api/v1/crawl/c1?cursor=abc", "cursor": "abc"},
                   {"id": "c1", "status": "done", "data": [{"url": "https://d.test/b"}], "cursor": "def"})
    job = arc.crawl("https://d.test/", limit=2)
    assert isinstance(job, Crawl) and job.webhook_secret == "whsec_x"
    assert [p["url"] for p in job.pages(poll=0)] == ["https://d.test/a", "https://d.test/b"]
    assert job.status == "done"
    assert "cursor=abc" in str(arc.calls[2].url)


def test_a_web_search_posts_its_body_with_the_key_and_polls_by_id():
    arc = scripted({"id": "s1", "status": "running", "data": []},
                   {"id": "s1", "status": "done", "data": [{"url": "https://a.test/"}]})
    out = arc.web_search("mesh arc", limit=3, include_domains=["a.test"], scrape=True, poll=0, idempotency_key="srch-1")
    assert out["data"] == [{"url": "https://a.test/"}]
    post, poll = arc.calls
    assert (post.method, post.url.path) == ("POST", "/api/v1/search")
    assert post.headers["Idempotency-Key"] == "srch-1"
    assert json.loads(post.content) == {"query": "mesh arc", "limit": 3, "includeDomains": ["a.test"],
                                        "scrape": {"formats": ["markdown"]}, "timeout": 60}
    assert (poll.method, str(poll.url)) == ("GET", "https://api.mesharc.dev/api/v1/search/s1")


def test_export_streams_to_a_file(tmp_path):
    arc = scripted({"rows": 1})
    out = arc.export("p1", str(tmp_path / "pages.jsonl"), dataset="pages")
    assert open(out).read() == '{"rows":1}' or json.load(open(out)) == {"rows": 1}
    assert "dataset=pages" in str(arc.calls[0].url)


def test_a_204_returns_none():
    arc = scripted((204, None))
    assert arc.revoke_key("k1") is None


def test_cancel_batch_deletes_the_scrape():
    arc = scripted((204, None))
    assert arc.cancel_batch("b1") is None
    assert (arc.calls[0].method, arc.calls[0].url.path) == ("DELETE", "/api/v1/scrape/b1")


def test_an_agent_run_posts_its_body_with_the_key_and_is_polled_by_id():
    arc = scripted((202, {"id": "a1", "kind": "agent", "status": "running", "data": None, "next": "/api/v1/agent/a1"}),
                   {"id": "a1", "kind": "agent", "status": "done", "data": {"text": "MeshArc"},
                    "sources": [{"url": "https://a.test/", "title": "A", "pageId": "p1"}], "next": None})
    run = arc.agent("who makes it?", max_credits=100, allowed_domains=["a.test"], timeout_s=10,
                    idempotency_key="agent-1")
    assert run.status == "running"
    assert run.wait(poll=0)["data"] == {"text": "MeshArc"}
    assert run.sources[0]["pageId"] == "p1"
    post, poll = arc.calls
    assert (post.method, post.url.path) == ("POST", "/api/v1/agent")
    assert post.headers["Idempotency-Key"] == "agent-1"
    assert json.loads(post.content) == {"prompt": "who makes it?", "maxCredits": 100, "allowedDomains": ["a.test"],
                                        "timeout": 10}
    assert (poll.method, str(poll.url)) == ("GET", "https://api.mesharc.dev/api/v1/agent/a1")


def test_an_expired_agent_run_is_a_410_error_and_ends_a_trace_quietly():
    arc = scripted((410, {"error": "this run has expired", "code": "expired"}))
    with pytest.raises(MeshArcError) as exc:
        arc.get_agent("a1")
    assert (exc.value.status, exc.value.code) == (410, "expired")
    arc = scripted({"id": "a1", "status": "running"}, (410, {"error": "this run has expired", "code": "expired"}))
    run = arc.get_agent("a1")
    assert list(run.trace(poll=0)) == []
    assert str(arc.calls[1].url) == "https://api.mesharc.dev/api/v1/agent/a1"


def test_continuing_a_run_posts_json_and_a_409_is_raised_with_its_code():
    arc = scripted((202, {"id": "a2", "status": "running", "continuesRunId": "a1"}),
                   (409, {"error": "this run was already continued, by run a2", "code": "conflict", "request_id": "req_c"}))
    first = AgentRun(arc._h, {"id": "a1", "status": "credit_limit"}).continue_(max_credits=500)
    assert (first.id, first.continues_run_id) == ("a2", "a1")
    post = arc.calls[0]
    assert (post.method, post.url.path) == ("POST", "/api/v1/agent/a1/continue")
    assert json.loads(post.content) == {"maxCredits": 500}
    with pytest.raises(MeshArcError) as exc:
        AgentRun(arc._h, {"id": "a1", "status": "credit_limit"}).continue_()
    assert (exc.value.status, exc.value.code, exc.value.request_id) == (409, "conflict", "req_c")
    assert len(arc.calls) == 2


def test_the_monitors_namespace_sends_its_bodies_as_json_with_the_key():
    arc = scripted((201, {"id": "m1", "kind": "search", "webhookSecret": "whsec_m"}),
                   {"id": "m1"}, {"id": "m1", "webhook": None},
                   (202, {"id": "r1", "status": "running"}))
    assert arc.monitors.create("search", {"query": "mesh arc", "sources": ["news"]}, "hourly",
                               idempotency_key="mon-1")["webhookSecret"] == "whsec_m"
    arc.monitors.update("m1", name=None, schedule="daily", status=None, webhook=None)
    arc.monitors.update("m1", webhook="")
    arc.monitors.run("m1", idempotency_key="run-1")
    create, update, remove, run = arc.calls
    assert (create.method, create.url.path, create.headers["Idempotency-Key"]) == ("POST", "/api/v1/monitors", "mon-1")
    assert json.loads(create.content) == {"kind": "search", "request": {"query": "mesh arc", "sources": ["news"]},
                                          "schedule": "hourly"}
    assert (update.method, update.url.path) == ("PATCH", "/api/v1/monitors/m1")
    assert json.loads(update.content) == {"schedule": "daily"}, "a None is not sent as null"
    assert json.loads(remove.content) == {"webhook": ""}, "the empty string is what removes the webhook"
    assert (run.method, run.url.path, run.headers["Idempotency-Key"]) == ("POST", "/api/v1/monitors/m1/run", "run-1")


def test_the_agent_list_puts_a_datetimes_zone_in_the_query_intact():
    arc = scripted({"data": [], "next": None})
    assert list(arc.agent_runs(since=datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc))) == []
    assert arc.calls[0].url.params["since"] == "2026-10-08T09:30:00+00:00"
