"""What an assistant reads before it calls a tool.

A tool is chosen and filled in from its definition alone: the description, the
parameters' descriptions and the hints on what it does to the world. Every
parameter went undescribed until 0.3.2, and directories that grade
definitions marked the whole server down for it. These keep it from
drifting back.
"""
import asyncio

import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")

# Every tool the server offers. Adding one is a line here, not a count to
# find and bump.
EXPECTED = {"scrape_urls", "map_site", "crawl_site", "web_search", "list_projects", "get_project",
            "create_project", "update_project", "delete_project", "list_runs", "list_pages", "get_page",
            "get_changes", "start_run", "get_job", "cancel_job"}

# The tools that only read what the workspace has stored. Everything else
# reaches a site or changes the workspace, and a read-only connection is
# refused it -- the same seven the read_only answer is about.
READ_ONLY = {"list_projects", "get_project", "list_runs", "list_pages", "get_page", "get_changes", "get_job"}


def _tools():
    return {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}


def test_every_tool_is_listed():
    # 0.4.0 folded five tools into the siblings they overlapped and added
    # delete_project, nineteen becoming fifteen; web_search makes sixteen.
    assert set(_tools()) == EXPECTED
    assert READ_ONLY < EXPECTED


def test_every_parameter_is_described():
    for name, t in _tools().items():
        for param, schema in (t.input_schema.get("properties") or {}).items():
            assert (schema.get("description") or "").strip(), f"{name}.{param} has no description"


def test_every_tool_has_a_title_and_a_description():
    for name, t in _tools().items():
        assert t.title and t.title != name and len(t.title) > len(name), name
        assert len((t.description or "").split()) >= 20, name


def test_the_hints_say_which_tools_only_read():
    for name, t in _tools().items():
        a = t.annotations
        assert a is not None, name
        assert a.read_only_hint is (name in READ_ONLY), name
        if name in READ_ONLY:
            assert a.destructive_hint is False and a.open_world_hint is False, name


def test_only_the_tools_that_overwrite_or_stop_are_destructive():
    # update_project replaces values; cancel_job stops work that cannot be
    # resumed where it was; delete_project removes a project and its history.
    # Nothing else changes what is already there.
    destructive = {n for n, t in _tools().items() if t.annotations.destructive_hint}
    assert destructive == {"update_project", "cancel_job", "delete_project"}


def test_fixed_choices_are_listed():
    tools = _tools()

    def enum(tool, param):
        schema = tools[tool].input_schema["properties"][param]
        options = [schema] + list(schema.get("anyOf") or [])
        return next(o["enum"] for o in options if "enum" in o)

    assert enum("get_job", "kind") == ["crawl", "run", "batch", "search"]
    # A web search cannot be stopped: the API has no cancel for one.
    assert enum("cancel_job", "kind") == ["crawl", "run", "batch"]
    assert enum("list_pages", "mode") == ["content", "selector"]
    for tool in ("create_project", "update_project"):
        assert enum(tool, "schedule") == ["manual", "hourly", "daily", "weekly"]


def test_each_description_states_the_access_its_hints_declare():
    # The description and the hints must never disagree: graders mark a
    # contradiction down to 1, and an assistant told a tool only reads would
    # call it on a read-only connection and be refused.
    for name, t in _tools().items():
        says_read_only = "Read-only and free" in t.description
        says_write = "Needs write access" in t.description or "Needs an admin API key" in t.description
        assert says_read_only == (name in READ_ONLY), name
        assert says_write == (name not in READ_ONLY), name


def test_list_projects_passes_on_each_projects_status(monkeypatch):
    # It asked the API for `health`, which no project has ever carried, so
    # every row said health: null. The project's state is `status`.
    class _Projects:
        @staticmethod
        def list():
            return [{"id": "p1", "name": "Docs", "status": "failing", "webhookSecret": "whsec_x"}]

    class _Client:
        projects = _Projects()

    monkeypatch.setattr(mcp_mod, "_client", lambda: _Client())
    rows = mcp_mod.list_projects()["projects"]
    assert rows[0]["status"] == "failing" and "health" not in rows[0]
    assert "webhookSecret" not in rows[0]


class _Crawl:
    def __init__(self, status):
        self.envelope = {"status": status}
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Cancels:
    """A client that records what cancel_job asked it to stop."""

    def __init__(self, crawl_status="running", batch_status="running", run_outcome="cancelling"):
        self.crawl = _Crawl(crawl_status)
        self._batch_status = batch_status
        self.stopped_batches = []
        outcome = run_outcome

        class _Runs:
            asked = []

            def cancel(self, project_id, run_id):
                self.asked.append((project_id, run_id))
                return {"id": run_id, "outcome": outcome}

        self.runs = _Runs()

    def get_crawl(self, crawl_id):
        return self.crawl

    def batch(self, batch_id, formats="markdown"):
        self.batch_formats = formats
        return {"id": batch_id, "status": self._batch_status}

    def cancel_batch(self, batch_id):
        self.stopped_batches.append(batch_id)


def test_cancel_job_stops_a_running_crawl_and_says_how(monkeypatch):
    c = _Cancels(crawl_status="running")
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.cancel_job("crawl", "c1")
    assert c.crawl.cancelled and out["outcome"] == "cancelling" and "nothing is deleted" in out["note"]


def test_cancel_job_leaves_a_finished_crawl_alone(monkeypatch):
    c = _Cancels(crawl_status="done")
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.cancel_job("crawl", "c1")
    assert not c.crawl.cancelled and out["outcome"] == "done"


def test_cancel_job_stops_a_queued_batch_outright(monkeypatch):
    c = _Cancels(batch_status="queued")
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.cancel_job("batch", "b1")
    assert c.stopped_batches == ["b1"] and out["outcome"] == "cancelled"
    # The status is read without the pages' bodies.
    assert c.batch_formats == "none"


def test_cancel_job_passes_on_the_runs_outcome(monkeypatch):
    c = _Cancels(run_outcome="done")
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.cancel_job("run", "r1", project_id="p1")
    assert c.runs.asked[-1] == ("p1", "r1") and out["outcome"] == "done"


def test_cancel_job_needs_a_runs_project(monkeypatch):
    c = _Cancels()
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    assert mcp_mod.cancel_job("run", "r1")["code"] == "validation"


def test_list_runs_keeps_what_picks_a_run_and_drops_the_bulk(monkeypatch):
    asked = []

    class _Runs:
        def list(self, project_id, limit=25):
            asked.append((project_id, limit))
            return [{"id": "r2", "status": "running", "trigger": "manual", "counts": {"ok": 3},
                     "linkGraph": {"orphans": 9}, "cfg": "v2", "engine": "http"}]

    class _Client:
        runs = _Runs()

    monkeypatch.setattr(mcp_mod, "_client", lambda: _Client())
    out = mcp_mod.list_runs("p1", limit=500)
    assert asked == [("p1", 100)]
    row = out["runs"][0]
    assert row["id"] == "r2" and row["status"] == "running" and row["counts"] == {"ok": 3}
    assert "linkGraph" not in row and "cfg" not in row and "engine" not in row
