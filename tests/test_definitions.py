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

# The tools that only read what the workspace has stored. Everything else
# reaches a site or changes the workspace, and a read-only connection is
# refused it -- the same nine the read_only answer is about.
READ_ONLY = {"list_projects", "describe_project_config", "get_project", "list_pages",
             "get_page", "get_changes", "search_pages", "get_job"}


def _tools():
    return {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}


def test_all_seventeen_are_listed():
    assert len(_tools()) == 17


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


def test_only_update_project_overwrites():
    destructive = {n for n, t in _tools().items() if t.annotations.destructive_hint}
    assert destructive == {"update_project"}


def test_fixed_choices_are_listed():
    tools = _tools()

    def enum(tool, param):
        schema = tools[tool].input_schema["properties"][param]
        options = [schema] + list(schema.get("anyOf") or [])
        return next(o["enum"] for o in options if "enum" in o)

    assert enum("get_job", "kind") == ["crawl", "run", "batch"]
    assert enum("search_pages", "mode") == ["content", "selector"]
    for tool in ("create_project", "keep_crawl_as_project", "update_project"):
        assert enum(tool, "schedule") == ["manual", "hourly", "daily", "weekly"]


def test_each_description_states_the_access_its_hints_declare():
    # The description and the hints must never disagree: graders mark a
    # contradiction down to 1, and an assistant told a tool only reads would
    # call it on a read-only connection and be refused.
    for name, t in _tools().items():
        says_read_only = "Works on a read-only connection" in t.description
        says_write = "Needs write access" in t.description
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
    rows = mcp_mod.list_projects()
    assert rows[0]["status"] == "failing" and "health" not in rows[0]
    assert "webhookSecret" not in rows[0]
