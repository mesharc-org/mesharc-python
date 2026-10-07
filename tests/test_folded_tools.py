"""The five tools 0.4.0 folded into their siblings, delete_project, and the
output schemas.

Each folded tool kept its own call to the API -- extract_url's
/playground/extract, keep_crawl_as_project's /crawl/{id}/keep, and so on --
and is now reached through an argument of the sibling it overlapped. These
check that every argument reaches the call it stands for, and that the
mistakes an assistant can now make (both seed and crawl_id, full=true on a
list) are refused with a reason rather than guessed at.
"""
import asyncio

import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")
from mesharc import MeshArcError  # noqa: E402


class _Projects:
    def __init__(self, name="Docs", delete_error=None):
        self.name = name
        self.delete_error = delete_error
        self.deleted = []
        self.created = []

    def get(self, project_id):
        return {"id": project_id, "name": self.name, "webhookSecret": "whsec_x"}

    def delete(self, project_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(project_id)

    def create(self, seed, name=None, schedule="manual", config=None):
        self.created.append((seed, name, schedule, config))
        return {"id": "p9", "seed": seed}


class _Runs:
    def __init__(self):
        self.waited = []

    def wait(self, project_id, run_id, timeout=3600):
        self.waited.append((project_id, run_id, timeout))
        return {"id": run_id, "status": "complete"}

    def start(self, project_id, wait=False, timeout=3600):
        return {"id": "r-full", "status": "running", "queued": True}


class _Crawl:
    def __init__(self):
        self.kept = None

    def keep(self, name=None, schedule=None):
        self.kept = (name, schedule)
        return {"id": "p-kept", "name": name or "From crawl"}


class _Client:
    """Records which API call each argument led to."""

    def __init__(self, **kw):
        self.projects = _Projects(**kw)
        self.runs = _Runs()
        self.crawl = _Crawl()
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def extract(self, url, config=None):
        self.calls.append(("extract", url, config))
        return {"id": "x1", "status": "done", "page": {"url": url, "markdown": "m" * 20_000, "links": ["a"]}}

    def scrape(self, url, config=None, formats="markdown", idempotency_key=None, timeout=None):
        self.calls.append(("scrape", url, formats))
        return {"url": url, "markdown": "body"}

    def get_crawl(self, crawl_id):
        self.calls.append(("get_crawl", crawl_id))
        return self.crawl

    def meta(self):
        return {"configDefaults": {"max_pages": 500, "render_js": "auto", "obscure_key": 1}}

    def recrawl(self, project_id, urls):
        self.calls.append(("recrawl", project_id, list(urls)))
        return {"id": "r-scoped", "status": "running", "queued": True, "scope": len(urls)}

    def search(self, project_id, q, mode="content", run_id=None):
        self.calls.append(("search", project_id, q, mode, run_id))
        return {"runId": "r1", "hits": [{"url": "u", "count": 2, "snippet": "s"}], "scanned": 9}

    def pages(self, project_id, run_id=None):
        self.calls.append(("pages", project_id, run_id))
        return {"runId": "r1", "pages": [{"url": f"u{i}"} for i in range(600)]}


@pytest.fixture
def client(monkeypatch):
    c = _Client()
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    return c


# -- scrape_urls(full=true): what extract_url was --------------------------------

def test_full_reads_one_page_through_the_extract_call(client):
    out = mcp_mod.scrape_urls(["https://example.com/a"], full=True)
    assert client.calls == [("extract", "https://example.com/a", {"parse_documents": True})]
    assert "links" not in out["page"], "the link list stays out, as it did for extract_url"
    assert "cap for one page" in out["page"]["markdown"]


def test_full_refuses_a_list_rather_than_reading_one_of_it(client):
    out = mcp_mod.scrape_urls(["https://example.com/a", "https://example.com/b"], full=True)
    assert out["code"] == "validation" and client.calls == []


def test_without_full_one_url_is_still_a_plain_scrape(client):
    out = mcp_mod.scrape_urls(["https://example.com/a"])
    assert client.calls[0][0] == "scrape" and out["status"] == "done"


# -- create_project(crawl_id=...): what keep_crawl_as_project was ----------------

def test_a_crawl_id_keeps_the_crawl(client):
    out = mcp_mod.create_project(crawl_id="c1", name="Blog", schedule="weekly")
    assert ("get_crawl", "c1") in client.calls and client.crawl.kept == ("Blog", "weekly")
    assert out["id"] == "p-kept" and client.projects.created == []


def test_a_seed_creates_a_project(client):
    mcp_mod.create_project(seed="example.com", config={"max_pages": 10})
    assert client.projects.created == [("example.com", None, "manual", {"max_pages": 10})]


@pytest.mark.parametrize("args", [{}, {"seed": "example.com", "crawl_id": "c1"}])
def test_seed_and_crawl_id_are_one_or_the_other(client, args):
    out = mcp_mod.create_project(**args)
    assert out["code"] == "validation" and client.calls == [] and client.projects.created == []


def test_a_kept_crawl_takes_no_config(client):
    out = mcp_mod.create_project(crawl_id="c1", config={"max_pages": 10})
    assert out["code"] == "validation" and client.crawl.kept is None


# -- get_project() with no id: what describe_project_config was ------------------

def test_no_project_id_reads_the_settings_reference(client):
    out = mcp_mod.get_project()
    keys = {row["key"]: row for row in out["settings"]}
    assert keys["max_pages"]["default"] == 500 and "meaning" in keys["include_paths"]
    assert out["other_keys"] == ["obscure_key"]
    assert out["schedules"] == ["manual", "hourly", "daily", "weekly"]


def test_a_project_id_reads_that_project_without_its_secret(client):
    out = mcp_mod.get_project("p1")
    assert out["id"] == "p1" and "webhookSecret" not in out


# -- start_run(urls=...): what recrawl_pages was --------------------------------

def test_urls_make_a_scoped_run(client):
    out = mcp_mod.start_run("p1", urls=["/pricing"])
    assert client.calls == [("recrawl", "p1", ["/pricing"])] and out["id"] == "r-scoped"


def test_a_scoped_run_can_be_waited_for(client):
    out = mcp_mod.start_run("p1", urls=["/pricing"], wait=True)
    assert client.runs.waited == [("p1", "r-scoped", 3600)] and out["status"] == "complete"


def test_no_urls_is_a_full_run(client):
    out = mcp_mod.start_run("p1")
    assert client.calls == [] and out["id"] == "r-full"


def test_an_empty_list_is_not_a_full_run(client):
    # The API refuses an empty list, as it refused recrawl_pages([]). Read as
    # "no urls", it would have crawled -- and charged for -- the whole site.
    mcp_mod.start_run("p1", urls=[])
    assert client.calls == [("recrawl", "p1", [])]


# -- list_pages(q=...): what search_pages was -----------------------------------

def test_q_searches(client):
    out = mcp_mod.list_pages("p1", run_id="r1", q="form#signup", mode="selector")
    assert client.calls == [("search", "p1", "form#signup", "selector", "r1")] and out["hits"]


def test_no_q_lists_and_keeps_the_500_cap(client):
    out = mcp_mod.list_pages("p1")
    assert client.calls == [("pages", "p1", None)] and len(out["pages"]) == 500


# -- delete_project ---------------------------------------------------------------

def test_delete_needs_the_name_repeated_exactly(monkeypatch):
    c = _Client(name="Docs")
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.delete_project("p1", confirm_name="docs")
    assert out["code"] == "validation" and c.projects.deleted == []
    assert "Docs" not in out["error"], "the refusal must not hand the name back to retry with"


def test_delete_with_the_right_name_deletes(monkeypatch):
    c = _Client(name="Docs")
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.delete_project("p1", confirm_name="Docs")
    assert c.projects.deleted == ["p1"] and out["deleted"] is True


def test_a_key_without_admin_is_told_where_to_delete(monkeypatch):
    # A connected app holds read and write, never admin, so this is what every
    # hosted caller hears -- and it has to say where the delete can be done.
    c = _Client(name="Docs", delete_error=MeshArcError(403, "this needs the admin role", "forbidden"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.delete_project("p1", confirm_name="Docs")
    assert out["code"] == "admin_only" and "MeshArc app" in out["error"] and c.projects.deleted == []


# -- output schemas -----------------------------------------------------------------

def _listed():
    return {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}


def test_every_tool_describes_its_answer_without_filtering_it():
    for name, t in _listed().items():
        schema = t.output_schema
        assert schema and schema.get("type") == "object", name
        # Extra fields pass: the schema describes the answer, it must never
        # drop a field the API adds later.
        assert schema.get("additionalProperties") is True, name
        assert not schema.get("required"), name
        props = schema.get("properties") or {}
        assert "error" in props, f"{name}: an error answer has to fit the schema too"
        assert all((p.get("description") or "").strip() for p in props.values()), name


def test_an_error_and_an_unknown_field_both_come_through_a_real_call(monkeypatch):
    class _Failing(_Client):
        def search(self, *a, **kw):
            raise MeshArcError(404, "no such project", "not_found")

        def pages(self, project_id, run_id=None):
            return {"runId": "r1", "pages": [], "aFieldAddedLater": {"n": 1}}

    c = _Failing()
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    bad = asyncio.run(mcp_mod.server.call_tool("list_pages", {"project_id": "p1", "q": "x"}))
    assert bad.structured_content == {"error": "no such project", "status": 404}
    good = asyncio.run(mcp_mod.server.call_tool("list_pages", {"project_id": "p1"}))
    assert good.structured_content["aFieldAddedLater"] == {"n": 1}
