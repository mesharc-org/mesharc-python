"""The client against a scripted transport: no network, no key."""

import json

import httpx
import pytest

from mesharc import Crawl, MeshArc, MeshArcError, MeshArcTimeoutError, __version__


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


def test_export_streams_to_a_file(tmp_path):
    arc = scripted({"rows": 1})
    out = arc.export("p1", str(tmp_path / "pages.jsonl"), dataset="pages")
    assert open(out).read() == '{"rows":1}' or json.load(open(out)) == {"rows": 1}
    assert "dataset=pages" in str(arc.calls[0].url)


def test_a_204_returns_none():
    arc = scripted((204, None))
    assert arc.revoke_key("k1") is None
