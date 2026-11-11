"""A run's and a project's counts are numbers, under one name each.

The API's `pages`, `changed`, `errors` and `credits` are what its screens
print: "1,284", and a dash where there is nothing to compare. Some have the
number beside them as `pagesN` and `changedN`. The tools passed both through,
so an assistant read `"pages": "3"` beside `"pagesN": 3` and `"changed": "—"`
(MCP re-test, 11 Nov 2026). Every tool that answers with a run or a project
now gives the number, or null, under the plain name.
"""
import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")

RUN = {"id": "r1", "status": "complete", "queued": False, "queuedN": 0, "trigger": "manual", "scope": 0,
       "pages": "1,284", "pagesN": 1284, "changed": "—", "changedN": None, "errors": "12",
       "counts": {"ok": 1272, "blocked": 12}, "coverage": 100, "stop": "", "startedAt": "2026-11-11T09:00:00+00:00"}
PROJECT = {"id": "p1", "name": "Docs", "pages": "3", "pagesN": 3, "credits": "—", "config": {},
           "lastRun": dict(RUN, pages="3", pagesN=3, changed="1", changedN=1, errors="0")}


class _Runs:
    def start(self, project_id, wait=False, **_kw):
        return dict(RUN)

    def get(self, project_id, run_id):
        return dict(RUN)

    def list(self, project_id, limit=25):
        return [dict(RUN), dict(RUN, id="r0", changed="7", changedN=7)]


class _Projects:
    def get(self, project_id):
        return dict(PROJECT)

    def create(self, seed, **_kw):
        return dict(PROJECT)

    def update(self, project_id, **_kw):
        return dict(PROJECT)


class _Arc:
    runs, projects = _Runs(), _Projects()

    def webhook_deliveries(self, project_id, limit=10):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


@pytest.fixture(autouse=True)
def client(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc())


def _plain(row):
    return {k: row.get(k) for k in ("pages", "changed", "errors")}


def test_a_run_gives_its_counts_as_numbers_and_null_where_there_is_nothing_to_compare():
    for run in (mcp_mod.start_run("p1", wait=True), mcp_mod.get_job("run", "r1", project_id="p1")):
        assert _plain(run) == {"pages": 1284, "changed": None, "errors": 12}
        assert "pagesN" not in run and "changedN" not in run
        assert run["queued"] is False and run["counts"] == {"ok": 1272, "blocked": 12}, "not counts: left alone"


def test_list_runs_uses_the_same_names():
    runs = mcp_mod.list_runs("p1")["runs"]
    assert [_plain(r) for r in runs] == [{"pages": 1284, "changed": None, "errors": 12},
                                         {"pages": 1284, "changed": 7, "errors": 12}]
    assert all("pagesN" not in r for r in runs)


def test_a_project_gives_them_too_with_its_last_run():
    for project in (mcp_mod.get_project("p1"), mcp_mod.create_project(seed="https://x.test/"),
                    mcp_mod.update_project("p1", name="Docs")):
        assert project["pages"] == 3 and "pagesN" not in project
        assert project["credits"] is None, "the dash is no number"
        assert _plain(project["lastRun"]) == {"pages": 3, "changed": 1, "errors": 0}


def test_a_value_that_is_not_a_count_is_left_as_it_is():
    """A crawl's `pages` is its list of pages; an error's `status` is its code."""
    rows = [{"url": "https://x.test/a"}]
    assert mcp_mod._counts({"pages": rows, "status": 404})["pages"] is rows
    assert mcp_mod._counts("not a row") == "not a row"
    assert [mcp_mod._as_count(v) for v in ("1,284", "0", "—", "", None, 7, True)] == [1284, 0, None, None, None, 7, None]
